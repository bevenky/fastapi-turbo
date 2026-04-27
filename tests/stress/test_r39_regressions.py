"""R39 audit follow-ups — TestClient ``__enter__`` / ``__exit__``
propagate lifespan startup / shutdown failures (the swallow site
distinct from R37/R38), the raw-ASGI middleware lifespan-shutdown
chain surfaces failures up through ``_stop_lifespan_mw_chain``, the
in-process dispatcher preserves pydantic ``ctx`` in 422 bodies, the
in-process dynamic-routes installer registers
``/docs/oauth2-redirect``, and ``_ASGISyncClientShim`` exposes
``base_url``."""
import contextlib

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 TestClient __enter__ / __exit__ propagate lifespan failures
# ────────────────────────────────────────────────────────────────────


def test_testclient_enter_propagates_lifespan_startup_failure():
    """Probe-confirmed: a ``@app.on_event('startup')`` handler
    raising RuntimeError did NOT surface from ``with
    TestClient(app):`` — the swallow at testclient.py:1013
    masked it. Subsequent requests against the partially-
    initialised app returned 200 silently. R39 removes the
    swallow and lets the original exception propagate so the
    ``with`` block fails immediately."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.on_event("startup")
    async def _s():
        raise RuntimeError("startup_failure_intentional_r39")

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    with pytest.raises(RuntimeError, match="startup_failure_intentional_r39"):
        with TestClient(app, in_process=True) as c:
            c.get("/ok")


def test_testclient_exit_propagates_lifespan_shutdown_failure():
    """Probe-confirmed: a lifespan ``@asynccontextmanager``
    whose ``__aexit__`` raises was silently caught at
    testclient.py:1031, so ``with TestClient(app):`` never saw
    the cleanup error. R39 captures the teardown exception and
    re-raises after the rest of the cleanup completes — server
    stop / cache eviction still happen, but the failure
    surfaces."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        raise RuntimeError("aexit_failure_intentional_r39")

    app = FastAPI(lifespan=lifespan)

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    with pytest.raises(RuntimeError, match="aexit_failure_intentional_r39"):
        with TestClient(app, in_process=True) as c:
            c.get("/ok")


# ────────────────────────────────────────────────────────────────────
# #2 raw-ASGI MW chain lifespan.shutdown.failed surfaces up
# ────────────────────────────────────────────────────────────────────


def test_stop_lifespan_mw_chain_raises_on_shutdown_failed_event():
    """Static check on ``_stop_lifespan_mw_chain``: the body must
    inspect the queued ``send_events`` for a
    ``lifespan.shutdown.failed`` entry AND the task's own
    exception state, then raise rather than returning True
    silently. Driving the actual MW chain end-to-end requires
    the full worker-loop + chain plumbing; the live integration
    is exercised via TestClient.__exit__ → _stop_lifespan_mw_chain
    in production.

    The earlier swallow at applications.py:6015 (``await
    state['task']`` wrapped in ``except BaseException: pass``)
    was the audit's specific finding — a static check on the
    new code shape catches a future regression that re-introduces
    the swallow."""
    import inspect

    from fastapi_turbo.applications import FastAPI as _FA
    src = inspect.getsource(_FA._stop_lifespan_mw_chain)
    # Must look at the last queued event for shutdown.failed.
    assert "lifespan.shutdown.failed" in src, src
    # Must capture the task's exception state (not silently pass).
    assert "task_exc" in src, src
    # Must explicitly raise — not just record the failure.
    assert "raise " in src, src


# ────────────────────────────────────────────────────────────────────
# #3 in-process 422 bodies preserve pydantic ``ctx``
# ────────────────────────────────────────────────────────────────────


def test_inprocess_422_preserves_pydantic_ctx_field():
    """Upstream FastAPI 422 responses include the constraint
    context that triggered the validation error — e.g. for a
    ``Query(min_length=1)`` violation, ``ctx == {'min_length':
    1}``. Earlier our in-process dispatcher stripped ``ctx`` in
    three sites (``applications.py:7908`` / ``:8182`` / ``:8213``)
    plus ``_route_helpers.py:673``. Across upstream FastAPI tests
    this drift caused ~70 of the 888 sandboxed-mode failures
    (R39 audit). R39 strips only the doc-link ``url`` field and
    keeps ``ctx``."""
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/q")
    def _q(foo: str = Query(min_length=1)):
        return {"foo": foo}

    with TestClient(app, in_process=True) as c:
        r = c.get("/q?foo=")
        assert r.status_code == 422, r.json()
        body = r.json()
        # Each error entry must carry a ``ctx`` field with the
        # constraint values.
        assert any(
            isinstance(e.get("ctx"), dict)
            and "min_length" in e["ctx"]
            for e in body["detail"]
        ), body


# ────────────────────────────────────────────────────────────────────
# #4 /docs/oauth2-redirect installed by in-process dynamic routes
# ────────────────────────────────────────────────────────────────────


def test_inprocess_dynamic_routes_install_oauth2_redirect():
    """The Rust ``run_server`` path registers
    ``/docs/oauth2-redirect`` for the Swagger UI OAuth2 callback;
    the in-process installer used to skip it, so upstream
    ``test_swagger_ui_oauth2_redirect`` returned 404 in
    sandboxed / ASGITransport runs. R39 adds the registration
    so both paths agree."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/h")
    async def _h():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/docs/oauth2-redirect")
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert "swaggerUIRedirectOauth2" in r.text or "oauth2" in r.text.lower(), r.text[:200]


# ────────────────────────────────────────────────────────────────────
# #5 _ASGISyncClientShim.base_url is reachable
# ────────────────────────────────────────────────────────────────────


def test_testclient_in_process_exposes_base_url():
    """``client.base_url`` is a Starlette TestClient idiom —
    upstream's ``test_custom_swagger_ui_redirect`` reads it.
    Our ``_ASGISyncClientShim`` stored ``_base_url`` but had no
    public ``base_url`` accessor, so ``hasattr(client,
    'base_url')`` returned False and the proxy ``__getattr__``
    raised ``AttributeError``. R39 adds a property forwarding
    to the underlying httpx client's ``base_url``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/x")
    async def _x():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        bu = c.base_url
        assert bu is not None
        # httpx URL str representation contains the base.
        assert "testserver" in str(bu) or "://" in str(bu), str(bu)
