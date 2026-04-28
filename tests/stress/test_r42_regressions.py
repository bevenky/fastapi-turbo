"""R42 audit follow-ups — yield-dep error propagation, multi-error
form 422, file-bytes coercion, JSON decode error shape, body
missing-field loc, validation-error endpoint context. Net
sandboxed-gate change: 243 → 155 failed (-88).
"""
from typing import Annotated

import pytest
from pydantic import BaseModel

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 yield-dep teardown sees handler exceptions via gen.throw()
# ────────────────────────────────────────────────────────────────────


def test_yield_dep_finally_runs_when_handler_raises():
    """FA contract: a yield-dep wrapping the request in
    ``try / except / finally`` must see handler exceptions via
    ``gen.throw()`` so its ``finally`` block runs. Earlier the
    teardown drove plain ``__anext__()`` AFTER
    ``_asgi_emit_exception`` (which re-raises for unhandled
    exceptions), so finally never executed."""
    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    state = {"v": "init"}

    async def dep_async():
        state["v"] = "started"
        try:
            yield "x"
        finally:
            state["v"] = "finalized"

    class _BoomError(Exception):
        pass

    app = FastAPI()

    @app.get("/boom")
    async def _h(_d: str = Depends(dep_async)):
        raise _BoomError()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(_BoomError):
            c.get("/boom")
    assert state["v"] == "finalized"


def test_yield_dep_except_arm_sees_thrown_exception():
    """When the handler raises a type the dep ``except`` matches,
    the dep's except arm fires."""
    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    seen: list = []

    class _DepErr(Exception):
        pass

    async def dep():
        try:
            yield "x"
        except _DepErr:
            seen.append("caught")
            raise

    app = FastAPI()

    @app.get("/x")
    async def _h(_d: str = Depends(dep)):
        raise _DepErr()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(_DepErr):
            c.get("/x")
    assert seen == ["caught"]


# ────────────────────────────────────────────────────────────────────
# #2 BG runs BEFORE yield-dep teardowns
# ────────────────────────────────────────────────────────────────────


def test_background_tasks_run_before_yield_dep_teardowns():
    """Background tasks see deps in pre-yield ("started") state.
    FA / Starlette ordering: response → bg → yield-dep teardown."""
    from fastapi_turbo import BackgroundTasks, Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    state = {"phase": None}

    async def dep_a():
        state["dep_a"] = "started"
        try:
            yield
        finally:
            state["dep_a"] = "finalized"

    def _bg_task():
        # When this runs, dep_a should still be in "started" state.
        state["bg_observed_dep_a"] = state["dep_a"]

    app = FastAPI()

    @app.get("/x")
    async def _h(bg: BackgroundTasks, _d: None = Depends(dep_a)):
        bg.add_task(_bg_task)
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/x")
        assert r.status_code == 200, r.text
    assert state["bg_observed_dep_a"] == "started"
    assert state["dep_a"] == "finalized"


# ────────────────────────────────────────────────────────────────────
# #3 functools.wraps wrapper around generator dep is detected
# ────────────────────────────────────────────────────────────────────


def test_wraps_wrapped_generator_dep_is_iterated():
    """A plain ``def wrapper(*a, **kw): return func(*a, **kw)``
    around a generator dep doesn't trip ``isgeneratorfunction`` —
    we must inspect the return value to detect the generator and
    drive it. Same for class instances whose ``__call__`` is a
    generator."""
    from functools import wraps

    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    def gen_dep():
        yield "from-gen"

    @wraps(gen_dep)
    def wrapper():
        return gen_dep()

    app = FastAPI()

    @app.get("/d")
    async def _h(v: str = Depends(wrapper)):
        return {"v": v}

    with TestClient(app, in_process=True) as c:
        r = c.get("/d")
        assert r.status_code == 200, r.text
        assert r.json() == {"v": "from-gen"}


# ────────────────────────────────────────────────────────────────────
# #4 outer endpoint form-missing accumulates all errors
# ────────────────────────────────────────────────────────────────────


def test_outer_form_missing_emits_all_in_one_422():
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/login")
    async def _login(
        a: Annotated[str, Form()],
        b: Annotated[str, Form()],
        c: Annotated[str, Form()],
    ):
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.post("/login")
        assert r.status_code == 422, r.text
        body = r.json()
        locs = {tuple(e["loc"]) for e in body["detail"] if e["type"] == "missing"}
        assert ("body", "a") in locs, body
        assert ("body", "b") in locs, body
        assert ("body", "c") in locs, body


# ────────────────────────────────────────────────────────────────────
# #5 list[bytes] file param reads upload contents
# ────────────────────────────────────────────────────────────────────


def test_list_bytes_file_param_reads_upload_contents():
    from fastapi_turbo import FastAPI, File
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/files")
    async def _f(files: Annotated[list[bytes], File()]):
        return {"sizes": [len(f) for f in files]}

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/files",
            files=[
                ("files", ("a.txt", b"hello", "text/plain")),
                ("files", ("b.txt", b"world!", "text/plain")),
            ],
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"sizes": [5, 6]}, r.json()


# ────────────────────────────────────────────────────────────────────
# #6 JSON decode error shape (msg + ctx)
# ────────────────────────────────────────────────────────────────────


def test_json_decode_error_uses_msg_plus_ctx():
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        x: int

    app = FastAPI()

    @app.post("/items")
    async def _h(item: _Item):
        return item.x

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/items",
            content="{not-json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        e = body["detail"][0]
        assert e["type"] == "json_invalid", body
        assert e["msg"] == "JSON decode error", body
        assert isinstance(e.get("ctx"), dict), body
        assert "error" in e["ctx"], body


# ────────────────────────────────────────────────────────────────────
# #7 embed=True body missing emits ["body", field]
# ────────────────────────────────────────────────────────────────────


def test_body_embed_true_missing_emits_body_field_loc():
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        name: str

    app = FastAPI()

    @app.post("/it")
    async def _h(item: Annotated[_Item, Body(embed=True)]):
        return item

    with TestClient(app, in_process=True) as c:
        r = c.post("/it")
        assert r.status_code == 422, r.text
        body = r.json()
        e = body["detail"][0]
        assert e["loc"] == ["body", "item"], body


# ────────────────────────────────────────────────────────────────────
# #8 RequestValidationError carries endpoint context
# ────────────────────────────────────────────────────────────────────


def test_request_validation_error_carries_endpoint_function():
    """User exception handlers can log file/line/function alongside
    the validation errors. Earlier our in-process dispatcher raised
    RVEs without ``endpoint_ctx`` so ``str(exc)`` showed only the
    error list."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.exceptions import RequestValidationError
    from fastapi_turbo.testclient import TestClient

    captured: dict = {}

    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def _h(_req, exc):
        captured["fn"] = exc.endpoint_function
        captured["str"] = str(exc)
        from fastapi_turbo.responses import JSONResponse
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @app.get("/users/{user_id}")
    async def get_user(user_id: int):
        return {"user_id": user_id}

    with TestClient(app, in_process=True) as c:
        c.get("/users/notanumber")

    assert captured.get("fn") == "get_user", captured
    assert "get_user" in captured.get("str", ""), captured


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
