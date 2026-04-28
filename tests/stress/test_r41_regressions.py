"""R41 audit follow-ups — three more in-process dispatcher parity
fixes uncovered while running upstream FastAPI's full test suite
under a sandboxed env (loopback bind kernel-denied, ASGITransport
forced). Together they fix the entire OAuth2 / security-tutorials
bucket. Net sandboxed-gate change: 455 → 243 failed (-212).

1. Async ``__call__`` on a class instance dep (e.g. FA's ``OAuth2``
   security schemes) is detected by inspecting the return value, not
   only by ``iscoroutinefunction(actual_fn)`` — which returns False
   on the instance itself even when its ``__call__`` is ``async
   def``.

2. Form / file params declared on a ``Depends(...)`` callable's
   ``__init__`` (e.g. ``OAuth2PasswordRequestFormStrict.__init__``
   ⇒ ``grant_type`` / ``username`` / ``password``) are now
   extracted in the dep resolver. Earlier they fell through to the
   parameter-default fallback and required fields raised
   ``TypeError: missing keyword-only argument`` on dep
   instantiation. All missing required fields surface in one 422 —
   matches FA.

3. The form/multipart parser fires when a Depends(...) somewhere on
   the route declares form params, not only when the OUTER endpoint
   does. The earlier gate check missed the OAuth2 deps' inner form
   params, leaving ``form_fields`` empty and producing spurious 422
   ``missing`` errors for fields the request actually carried.
"""
from typing import Annotated

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 async __call__ on a class instance is awaited
# ────────────────────────────────────────────────────────────────────


def test_async_call_on_class_instance_dep_is_awaited():
    """``OAuth2.__call__`` is ``async def`` but
    ``iscoroutinefunction(<oauth_instance>)`` returns False — only
    the bound ``__call__`` is async. Earlier the dep resolver fell
    into the sync branch and handed the user fn the unawaited
    coroutine. R41 detects via ``iscoroutine(val)`` after the call."""
    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    class _AsyncCallable:
        async def __call__(self, x: str = "hello"):
            return f"async-{x}"

    dep = _AsyncCallable()
    app = FastAPI()

    @app.get("/d")
    async def _d(v: str = Depends(dep)):
        return {"v": v}

    with TestClient(app, in_process=True) as c:
        r = c.get("/d")
        assert r.status_code == 200, r.text
        assert r.json() == {"v": "async-hello"}, r.json()


# ────────────────────────────────────────────────────────────────────
# #2 dep callables with form params on __init__
# ────────────────────────────────────────────────────────────────────


def test_dep_callable_with_form_params_extracts_form_data():
    """``Depends(SomeClass)`` where ``SomeClass.__init__`` declares
    ``Form(...)`` params extracts them from the request body. Earlier
    the dep resolver had no ``form`` / ``file`` branch and required
    Form() fields raised TypeError when the dep was constructed."""
    from fastapi_turbo import Depends, FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    class _Login:
        def __init__(
            self,
            grant_type: Annotated[str, Form()],
            username: Annotated[str, Form()],
            password: Annotated[str, Form()],
        ):
            self.grant_type = grant_type
            self.username = username
            self.password = password

    app = FastAPI()

    @app.post("/login")
    async def _login(form: Annotated[_Login, Depends()]):
        return {
            "grant_type": form.grant_type,
            "username": form.username,
            "password": form.password,
        }

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/login",
            data={
                "grant_type": "password",
                "username": "alice",
                "password": "secret",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "grant_type": "password",
            "username": "alice",
            "password": "secret",
        }


def test_dep_callable_form_missing_returns_all_errors_in_one_422():
    """Multiple required form fields missing on a dep callable must
    surface as ONE 422 with all entries — matches FA. Earlier the
    dep resolver short-circuited on the first missing field and the
    client only saw the first error."""
    from fastapi_turbo import Depends, FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    class _Login2:
        def __init__(
            self,
            a: Annotated[str, Form()],
            b: Annotated[str, Form()],
            c: Annotated[str, Form()],
        ):
            self.a, self.b, self.c = a, b, c

    app = FastAPI()

    @app.post("/login2")
    async def _login(_form: Annotated[_Login2, Depends()]):
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.post("/login2", data={})
        assert r.status_code == 422, r.text
        body = r.json()
        # All 3 missing fields surface in one response.
        missing_aliases = {
            tuple(e["loc"])
            for e in body["detail"]
            if e["type"] == "missing"
        }
        assert ("body", "a") in missing_aliases, body
        assert ("body", "b") in missing_aliases, body
        assert ("body", "c") in missing_aliases, body


# ────────────────────────────────────────────────────────────────────
# #3 form parser fires for nested-dep form params
# ────────────────────────────────────────────────────────────────────


def test_form_parser_fires_when_only_inner_dep_declares_form():
    """When the outer endpoint takes ``Depends(SomeClass)`` and
    SomeClass.__init__ declares ``Form(...)`` params, the form
    parser must run even though no Form param appears on the outer
    handler. Earlier the gate check (``any(p.kind == 'form' for p
    in introspect_params)``) only saw the OUTER plan and skipped
    parsing — the dep resolver later saw an empty ``form_fields``
    and produced spurious 422 ``missing`` errors for fields the
    request actually carried."""
    from fastapi_turbo import Depends, FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    class _F:
        def __init__(self, x: Annotated[str, Form()]):
            self.x = x

    app = FastAPI()

    @app.post("/inner-form")
    async def _h(f: Annotated[_F, Depends()]):
        return {"x": f.x}

    with TestClient(app, in_process=True) as c:
        r = c.post("/inner-form", data={"x": "alpha"})
        assert r.status_code == 200, r.text
        assert r.json() == {"x": "alpha"}, r.json()


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
