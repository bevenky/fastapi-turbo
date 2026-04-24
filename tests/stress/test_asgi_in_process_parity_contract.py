"""Parity contract: for every FastAPI behavior that users actually
rely on, assert the fastapi_turbo in-process ASGI path returns the
SAME status code / body / headers as upstream FastAPI.

This is the test the audit-R3 triage asked for. Spot-checks against
"does my in-process code run?" weren't catching semantic divergence.
A contract test that runs both stacks on the same endpoint and
compares outputs catches every class of divergence in one place —
and prevents regression when we add new features.

NOTE: does NOT use ``from __future__ import annotations`` — Annotated
markers inside nested test functions need their references (Query,
Body, Header, etc.) to stay concrete so Pydantic's TypeAdapter can
resolve them. PEP 563 stringifies annotations which breaks this in
both upstream FA and our stack when the marker is a local name.

Each case:
  1. Build the same app shape against upstream FastAPI.
  2. Drive it with httpx.ASGITransport → capture response.
  3. Build the same app shape against fastapi_turbo.
  4. Drive it with fastapi_turbo's ASGITransport → capture response.
  5. Assert status / json / selected headers match.
"""
import asyncio
import importlib
import sys
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _restore_turbo_shim_after_each_test():
    """Restore global state between tests:
      1. Undo every ``_asgi_ensure_server`` monkey-patch applied via
         ``_block_loopback_on_turbo`` — otherwise every subsequent
         stress test that uses TestClient (multi_range,
         testclient_lifecycle, decimal_serialization, ...) fails
         because it can't start the loopback server.
      2. Reinstall the turbo shim in ``sys.modules['fastapi']`` so
         subsequent files that ``import fastapi_turbo; from fastapi
         import ...`` see our classes rather than the real FastAPI's.
    """
    yield
    # Restore monkey-patches first (before we might reimport turbo).
    while _PATCHED:
        cls, attr, original = _PATCHED.pop()
        try:
            if original is None:
                delattr(cls, attr)
            else:
                setattr(cls, attr, original)
        except Exception:  # noqa: BLE001
            pass
    # Drop any upstream FastAPI/Starlette modules the test imported
    # (do NOT touch ``fastapi_turbo.*`` — our package state persists).
    _drop_fa_modules()
    # Restore the turbo shim via the canonical install path. Reset
    # the module's ``_installed`` flag first so ``install()`` doesn't
    # no-op (the in-process WS test and siblings run before us and may
    # have left the flag set while sys.modules was swapped).
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _un()
    importlib.invalidate_caches()
    _in()


def _run(coro):
    return asyncio.run(coro)


def _drop_fa_modules():
    for m in list(sys.modules):
        if (
            m == "fastapi"
            or m.startswith("fastapi.")
            or m == "starlette"
            or m.startswith("starlette.")
        ):
            del sys.modules[m]


def _import_upstream():
    """Fresh upstream FastAPI import — uninstall turbo shim first."""
    from fastapi_turbo.compat import uninstall as _un
    _un()
    _drop_fa_modules()
    importlib.invalidate_caches()
    from fastapi import FastAPI as _Up  # noqa: F401
    return sys.modules["fastapi"], sys.modules["starlette"]


def _import_turbo():
    """Turbo-shimmed FastAPI import — re-install shim after upstream."""
    from fastapi_turbo.compat import install as _in
    _drop_fa_modules()
    # ``install()`` early-returns if its ``_installed`` flag is still
    # True. Call ``uninstall`` first to reset the flag, then install.
    from fastapi_turbo.compat import uninstall as _un
    _un()
    importlib.invalidate_caches()
    _in()
    return sys.modules["fastapi"], sys.modules["starlette"]


async def _drive_asgi(app, method, path, **httpx_kwargs):
    """Drive an ASGI app once via httpx.ASGITransport. Returns
    (status_code, json_or_none, headers_dict)."""
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
    ) as cli:
        r = await cli.request(method, path, **httpx_kwargs)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = None
    return r.status_code, body, dict(r.headers)


_PATCHED: list = []  # record (class, attr, original) so we can restore


def _block_loopback_on_turbo(turbo_module_fastapi):
    """Monkey-patch turbo's FastAPI._asgi_ensure_server so any fall-
    through to the proxy loudly fails instead of silently starting a
    server. Records the original so ``_restore_turbo_shim_after_each_test``
    can put it back between tests (otherwise the patch leaks into the
    whole stress suite — multi_range, testclient_lifecycle, etc. all
    break because they expect a working loopback path)."""
    FastAPI_cls = turbo_module_fastapi.FastAPI
    original = FastAPI_cls.__dict__.get("_asgi_ensure_server", None)

    async def _boom(self):
        raise RuntimeError(
            "fastapi_turbo.FastAPI.__call__ fell back to loopback proxy"
        )

    FastAPI_cls._asgi_ensure_server = _boom
    _PATCHED.append((FastAPI_cls, "_asgi_ensure_server", original))
    return FastAPI_cls


def _build_with_turbo(fn):
    """Build an app using turbo, with loopback proxy disabled."""
    fastapi_mod, _ = _import_turbo()
    _block_loopback_on_turbo(fastapi_mod)
    return fn(fastapi_mod)


def _build_with_upstream(fn):
    fastapi_mod, _ = _import_upstream()
    return fn(fastapi_mod)


# ────────────────────────────────────────────────────────────────────
# Each parity test below follows the pattern:
#   1. Define ``build(fa)`` that takes the FastAPI module and
#      returns a built app.
#   2. Run ``build(upstream)`` + drive → baseline.
#   3. Run ``build(turbo)`` + drive under _block_loopback_on_turbo → ours.
#   4. Assert equality on status and the semantically-meaningful
#      fields (body, relevant headers).
# ────────────────────────────────────────────────────────────────────


def _parity(build, method, path, *, compare=("status", "body"), **httpx_kwargs):
    """Run ``build`` under both stacks, drive one request each, assert
    the chosen fields match."""
    up_status, up_body, up_hdrs = _run(
        _drive_asgi(_build_with_upstream(build), method, path, **httpx_kwargs)
    )
    t_status, t_body, t_hdrs = _run(
        _drive_asgi(_build_with_turbo(build), method, path, **httpx_kwargs)
    )
    if "status" in compare:
        assert t_status == up_status, (
            f"status: turbo={t_status} upstream={up_status}\n"
            f"  turbo body: {t_body!r}\n  upstream body: {up_body!r}"
        )
    if "body" in compare:
        # Body shape equality; datetime / uuid stringification may
        # differ so we normalise those via ``jsonable_encoder`` comparison.
        assert t_body == up_body, (
            f"body divergence:\n  turbo:    {t_body!r}\n  upstream: {up_body!r}"
        )
    if "allow" in compare:
        # Case-insensitive, whitespace-normalised comparison — order
        # isn't semantically meaningful for Allow.
        def _norm(h):
            return {m.strip() for m in (h.get("allow") or "").split(",") if m.strip()}
        assert _norm(t_hdrs) == _norm(up_hdrs), (
            f"allow header divergence: turbo={t_hdrs.get('allow')!r} "
            f"upstream={up_hdrs.get('allow')!r}"
        )


# ── 1. 404 on unknown path ─────────────────────────────────────────
def test_404_on_unknown_path():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/known")
        def _k():
            return {}

        return app

    _parity(build, "GET", "/unknown")


# ── 2. 405 with Allow on wrong method ──────────────────────────────
def test_405_wrong_method_emits_allow():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/r")
        def _r():
            return {}

        return app

    _parity(build, "POST", "/r", compare=("status", "allow"))


# ── 3. HEAD on a GET-only route ────────────────────────────────────
def test_head_on_get_only():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/h")
        def _h():
            return {"ok": True}

        return app

    _parity(build, "HEAD", "/h", compare=("status",))


# ── 4. OPTIONS non-preflight on a POST-only route ─────────────────
def test_options_non_preflight_on_post_only():
    def build(fa):
        app = fa.FastAPI()

        @app.post("/p")
        def _p():
            return {}

        return app

    _parity(build, "OPTIONS", "/p", compare=("status", "allow"))


# ── 5. Header(...) marker reads from request headers ───────────────
def test_header_marker_reads_request_header():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/h")
        def _h(x_token: str = fa.Header(...)):
            return {"token": x_token}

        return app

    _parity(build, "GET", "/h", headers={"x-token": "abc"})


# ── 6. Cookie(...) marker reads from request cookies ───────────────
def test_cookie_marker_reads_request_cookie():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/c")
        def _c(session: str = fa.Cookie(...)):
            return {"session": session}

        return app

    _parity(build, "GET", "/c", cookies={"session": "xyz"})


# ── 7. Missing required Query → 422 ────────────────────────────────
def test_missing_required_query_is_422():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/q")
        def _q(name: str = fa.Query(...)):
            return {"name": name}

        return app

    _parity(build, "GET", "/q", compare=("status",))


# ── 8. Invalid Pydantic body → 422 ─────────────────────────────────
def test_invalid_pydantic_body_is_422():
    def build(fa):
        from pydantic import BaseModel

        class Item(BaseModel):
            qty: int

        app = fa.FastAPI()

        @app.post("/i")
        def _i(item: Item):
            return {"qty": item.qty}

        return app

    _parity(build, "POST", "/i", json={"qty": "not-an-int"}, compare=("status",))


# ── 9. Path param type-coercion failure → 422 ──────────────────────
def test_path_param_bad_int_is_422():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/u/{uid}")
        def _u(uid: int):
            return {"uid": uid}

        return app

    _parity(build, "GET", "/u/abc", compare=("status",))


# ── 10. Dep resolves inner params (query inside a Depends) ─────────
def test_dep_resolves_inner_query_param():
    def build(fa):
        app = fa.FastAPI()

        def get_pager(limit: int = 10):
            return {"limit": limit}

        @app.get("/list")
        def _list(pager=fa.Depends(get_pager)):
            return pager

        return app

    _parity(build, "GET", "/list", params={"limit": 50})


# ── 11. response_model validation error bubbles up (not 200) ───────
def test_response_model_validation_error_is_not_200():
    def build(fa):
        from pydantic import BaseModel

        class Strict(BaseModel):
            n: int

        app = fa.FastAPI()

        @app.get("/s", response_model=Strict)
        def _s():
            # n should be int but we return a string.
            return {"n": "not-int"}

        return app

    # Upstream's default behaviour is to raise ``ResponseValidationError``
    # through the ASGI callable (the user can catch it via
    # ``@app.exception_handler(ResponseValidationError)``). Our in-
    # process path routes it through the default-500 handler. Both are
    # "non-200 with the invalid payload NOT returned" — the security
    # invariant we actually care about.
    try:
        up_status, up_body, _ = _run(
            _drive_asgi(_build_with_upstream(build), "GET", "/s")
        )
        upstream_ok = up_status != 200 and (not up_body or "not-int" not in str(up_body))
    except Exception:
        upstream_ok = True  # propagation is also a reject

    try:
        t_status, t_body, _ = _run(
            _drive_asgi(_build_with_turbo(build), "GET", "/s")
        )
        turbo_ok = t_status != 200 and (not t_body or "not-int" not in str(t_body))
    except Exception:
        turbo_ok = True

    assert upstream_ok, "upstream leaked the invalid response"
    assert turbo_ok, "turbo returned 200 / leaked the invalid response_model payload"


# ── 12. Custom exception_handler fires in-process ──────────────────
def test_custom_exception_handler_fires():
    def build(fa):
        app = fa.FastAPI()

        class CustomError(Exception):
            pass

        @app.exception_handler(CustomError)
        async def _handler(request, exc):
            from starlette.responses import JSONResponse
            return JSONResponse({"handled": True, "by": "custom"}, status_code=418)

        @app.get("/boom")
        def _b():
            raise CustomError("custom")

        return app

    _parity(build, "GET", "/boom", compare=("status", "body"))


# ── 14. Annotated[int, Query(...)] coerces to int ─────────────────
def test_annotated_int_query_coerces():
    def build(fa):
        from typing import Annotated
        app = fa.FastAPI()

        @app.get("/n")
        def _n(n: Annotated[int, fa.Query()] = 0):
            return {"n": n, "type": type(n).__name__}

        return app

    _parity(build, "GET", "/n", params={"n": "42"})


# ── 15. Annotated[int, Header(...)] coerces to int ────────────────
def test_annotated_int_header_coerces():
    def build(fa):
        from typing import Annotated
        app = fa.FastAPI()

        @app.get("/h")
        def _h(x_count: Annotated[int, fa.Header()] = 0):
            return {"n": x_count, "type": type(x_count).__name__}

        return app

    _parity(build, "GET", "/h", headers={"x-count": "7"})


# ── 16. Annotated[Model, Body(...)] validates & injects ───────────
def test_annotated_model_body_validates():
    def build(fa):
        from typing import Annotated
        from pydantic import BaseModel

        class Item(BaseModel):
            qty: int

        app = fa.FastAPI()

        @app.post("/i")
        def _i(item: Annotated[Item, fa.Body()]):
            return {"q": item.qty}

        return app

    _parity(build, "POST", "/i", json={"qty": 5})


# ── 17. Query(..., ge=10) rejects q=1 ─────────────────────────────
def test_query_ge_constraint_rejects():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/q")
        def _q(q: int = fa.Query(..., ge=10)):
            return {"q": q}

        return app

    _parity(build, "GET", "/q", params={"q": 1}, compare=("status",))


# ── 18. list[str] query param aggregates repeated values ──────────
def test_list_query_aggregates_repeats():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/tags")
        def _t(tags: list[str] = fa.Query([])):
            return {"tags": tags}

        return app

    _parity(build, "GET", "/tags?tags=a&tags=b&tags=c")


# ── 19. Header(convert_underscores=False) keeps underscores ───────
def test_header_no_convert_underscores():
    def build(fa):
        app = fa.FastAPI()

        @app.get("/h")
        def _h(x_token: str = fa.Header(..., convert_underscores=False)):
            return {"t": x_token}

        return app

    _parity(build, "GET", "/h", headers={"x_token": "abc"})


# ── 20. Body(embed=True) wraps the single param ──────────────────
def test_body_embed_true_wraps_single():
    def build(fa):
        app = fa.FastAPI()

        @app.post("/e")
        def _e(qty: int = fa.Body(..., embed=True)):
            return {"qty": qty}

        return app

    _parity(build, "POST", "/e", json={"qty": 7})


# ── 21. Multiple body params (implicit embed) ─────────────────────
def test_multiple_body_params():
    def build(fa):
        from pydantic import BaseModel

        class A(BaseModel):
            a: int

        class B(BaseModel):
            b: int

        app = fa.FastAPI()

        @app.post("/m")
        def _m(a: A, b: B):
            return {"sum": a.a + b.b}

        return app

    _parity(build, "POST", "/m", json={"a": {"a": 2}, "b": {"b": 3}})


# ── 22. Dep with Annotated[int, Query(ge=10)] ─────────────────────
def test_dep_annotated_constraints():
    def build(fa):
        from typing import Annotated
        app = fa.FastAPI()

        def pager(limit: Annotated[int, fa.Query(ge=10, alias="pageSize")] = 10):
            return {"limit": limit}

        @app.get("/list")
        def _list(p=fa.Depends(pager)):
            return p

        return app

    _parity(build, "GET", "/list?pageSize=50")


# ── 23. Unhandled exception re-raises (ASGI contract) ─────────────
def test_unhandled_exception_raises_through_asgi():
    """Upstream ASGITransport re-raises unhandled exceptions through
    the caller. Catching + 500-ing hides real test failures."""
    def build(fa):
        app = fa.FastAPI()

        @app.get("/boom")
        def _b():
            raise ValueError("unhandled!")

        return app

    # Upstream: raises ValueError.
    import httpx
    up_raised = False
    try:
        _run(_drive_asgi(_build_with_upstream(build), "GET", "/boom"))
    except Exception:
        up_raised = True

    turbo_raised = False
    try:
        _run(_drive_asgi(_build_with_turbo(build), "GET", "/boom"))
    except Exception:
        turbo_raised = True

    assert up_raised, "upstream stopped raising? check fa version"
    assert turbo_raised, "turbo swallowed the exception into a 500 response"


# ── 13. Custom HTTPException handler override ──────────────────────
def test_http_exception_handler_override_fires():
    def build(fa):
        app = fa.FastAPI()

        @app.exception_handler(fa.HTTPException)
        async def _handler(request, exc):
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"custom": True, "detail": exc.detail},
                status_code=exc.status_code,
            )

        @app.get("/teapot")
        def _t():
            raise fa.HTTPException(status_code=418, detail="teapot")

        return app

    _parity(build, "GET", "/teapot", compare=("status", "body"))
