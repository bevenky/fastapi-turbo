"""Shim completeness sweep — the plain-import contract.

The contract: line 1 of a user app is ``import fastapi_turbo``; EVERYTHING
else is plain ``from fastapi import ...`` / ``from starlette import ...`` and
it all just works under ``app.run()``.

This file is the IMPORT MATRIX half of that contract:

  * walk every ``fastapi.*`` and ``starlette.*`` submodule (pkgutil) after
    ``import fastapi_turbo`` — each must import without error;
  * door-critical entry points (routing, responses, testclient, staticfiles,
    middleware.*, websockets, background, security.*, encoders, sse, ...)
    must resolve to the exact object the turbo engine registered, from BOTH
    the real-package path and the ``fastapi_turbo.*`` path (single identity —
    the engine isinstance-checks these classes).

The runtime half (third-party clients under one ``app.run()`` boot) lives in
``tests/test_plain_import_stack.py``.
"""
from __future__ import annotations

import fastapi_turbo  # noqa: F401 — line 1 of the contract: installs the shim

import importlib
import pkgutil
import warnings

import pytest


def _walk_module_names() -> list[str]:
    """Every importable module name under the fastapi and starlette packages."""
    import fastapi
    import starlette

    names: set[str] = set()
    for pkg in (fastapi, starlette):
        names.add(pkg.__name__)
        for info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
            names.add(info.name)
    # ``fastapi.__main__`` executes the typer CLI on import — the one module
    # a user never imports and we must not either.
    return sorted(n for n in names if n.rsplit(".", 1)[-1] != "__main__")


def test_every_fastapi_and_starlette_submodule_imports():
    """After ``import fastapi_turbo``, every real-package submodule imports."""
    names = _walk_module_names()
    # Sanity: the walk actually found the packages (fastapi + starlette ship
    # ~80 modules; a broken walk returning a handful must fail loudly).
    assert len(names) >= 60, f"pkgutil walk looks broken: only {len(names)} modules: {names}"

    failures: list[str] = []
    with warnings.catch_warnings():
        # Upstream's own deprecation chatter (starlette.testclient/httpx,
        # starlette.middleware.wsgi — StarletteDeprecationWarning subclasses
        # UserWarning, not DeprecationWarning) — not a shim defect.
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", UserWarning)
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 — report every breakage
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "shim broke real-package imports:\n" + "\n".join(failures)


# (real_module, attr) -> (turbo_module, attr): both sides must be the SAME
# object — the engine registered exactly one class identity for each.
DOOR_CRITICAL = [
    # core application / routing
    ("fastapi", "FastAPI", "fastapi_turbo.applications", "FastAPI"),
    ("fastapi.applications", "FastAPI", "fastapi_turbo.applications", "FastAPI"),
    ("fastapi", "APIRouter", "fastapi_turbo.routing", "APIRouter"),
    ("fastapi.routing", "APIRouter", "fastapi_turbo.routing", "APIRouter"),
    ("fastapi.routing", "APIRoute", "fastapi_turbo.routing", "APIRoute"),
    # responses (single identity with the real starlette/fastapi classes)
    ("fastapi.responses", "Response", "fastapi_turbo.responses", "Response"),
    ("fastapi.responses", "JSONResponse", "fastapi_turbo.responses", "JSONResponse"),
    ("fastapi.responses", "StreamingResponse", "fastapi_turbo.responses", "StreamingResponse"),
    ("fastapi.responses", "HTMLResponse", "fastapi_turbo.responses", "HTMLResponse"),
    ("fastapi.responses", "FileResponse", "fastapi_turbo.responses", "FileResponse"),
    # test clients (httpx against the real Rust server)
    ("fastapi.testclient", "TestClient", "fastapi_turbo.testclient", "TestClient"),
    ("starlette.testclient", "TestClient", "fastapi_turbo.testclient", "TestClient"),
    # static files (Rust ServeDir marker)
    ("fastapi.staticfiles", "StaticFiles", "fastapi_turbo.staticfiles", "StaticFiles"),
    ("starlette.staticfiles", "StaticFiles", "fastapi_turbo.staticfiles", "StaticFiles"),
    # middleware.* (Tower markers + python HTTP-chain dispatchers)
    ("fastapi.middleware.cors", "CORSMiddleware", "fastapi_turbo.middleware.cors", "CORSMiddleware"),
    ("starlette.middleware.cors", "CORSMiddleware", "fastapi_turbo.middleware.cors", "CORSMiddleware"),
    ("fastapi.middleware.gzip", "GZipMiddleware", "fastapi_turbo.middleware.gzip", "GZipMiddleware"),
    ("starlette.middleware.gzip", "GZipMiddleware", "fastapi_turbo.middleware.gzip", "GZipMiddleware"),
    ("fastapi.middleware.trustedhost", "TrustedHostMiddleware",
     "fastapi_turbo.middleware.trustedhost", "TrustedHostMiddleware"),
    ("fastapi.middleware.httpsredirect", "HTTPSRedirectMiddleware",
     "fastapi_turbo.middleware.httpsredirect", "HTTPSRedirectMiddleware"),
    ("starlette.middleware.base", "BaseHTTPMiddleware", "fastapi_turbo.middleware.base", "BaseHTTPMiddleware"),
    ("starlette.middleware.sessions", "SessionMiddleware", "fastapi_turbo.middleware.sessions", "SessionMiddleware"),
    ("starlette.middleware.authentication", "AuthenticationMiddleware",
     "fastapi_turbo.authentication", "AuthenticationMiddleware"),
    # websockets
    ("fastapi", "WebSocket", "fastapi_turbo.websockets", "WebSocket"),
    ("fastapi.websockets", "WebSocket", "fastapi_turbo.websockets", "WebSocket"),
    ("fastapi.websockets", "WebSocketState", "fastapi_turbo.websockets", "WebSocketState"),
    ("starlette.websockets", "WebSocket", "fastapi_turbo.websockets", "WebSocket"),
    # background tasks (door drains these)
    ("fastapi", "BackgroundTasks", "fastapi_turbo.background", "BackgroundTasks"),
    ("fastapi.background", "BackgroundTasks", "fastapi_turbo.background", "BackgroundTasks"),
    ("starlette.background", "BackgroundTasks", "fastapi_turbo.background", "BackgroundTasks"),
    # security.* (turbo schemes: dict-shaped .model + pinned signatures)
    ("fastapi.security", "OAuth2PasswordBearer", "fastapi_turbo.security", "OAuth2PasswordBearer"),
    ("fastapi.security.oauth2", "OAuth2PasswordBearer", "fastapi_turbo.security", "OAuth2PasswordBearer"),
    ("fastapi.security", "HTTPBearer", "fastapi_turbo.security", "HTTPBearer"),
    ("fastapi.security.http", "HTTPBearer", "fastapi_turbo.security", "HTTPBearer"),
    ("fastapi.security.http", "HTTPBasic", "fastapi_turbo.security", "HTTPBasic"),
    ("fastapi.security.api_key", "APIKeyHeader", "fastapi_turbo.security", "APIKeyHeader"),
    ("fastapi.security.open_id_connect_url", "OpenIdConnect", "fastapi_turbo.security", "OpenIdConnect"),
    # encoders
    ("fastapi.encoders", "jsonable_encoder", "fastapi_turbo.encoders", "jsonable_encoder"),
    # SSE (engine isinstance-checks ServerSentEvent on yielded items)
    ("fastapi.sse", "ServerSentEvent", "fastapi_turbo.sse", "ServerSentEvent"),
    ("fastapi.sse", "EventSourceResponse", "fastapi_turbo.sse", "EventSourceResponse"),
    # upload / concurrency / templating bridges
    ("fastapi", "UploadFile", "fastapi_turbo.param_functions", "UploadFile"),
    ("fastapi.datastructures", "UploadFile", "fastapi_turbo.param_functions", "UploadFile"),
    ("starlette.datastructures", "UploadFile", "fastapi_turbo.param_functions", "UploadFile"),
    ("fastapi.concurrency", "run_in_threadpool", "fastapi_turbo.concurrency", "run_in_threadpool"),
    ("starlette.concurrency", "run_in_threadpool", "fastapi_turbo.concurrency", "run_in_threadpool"),
    ("fastapi.templating", "Jinja2Templates", "fastapi_turbo.templating", "Jinja2Templates"),
    ("starlette.templating", "Jinja2Templates", "fastapi_turbo.templating", "Jinja2Templates"),
]


@pytest.mark.parametrize(
    "real_mod,attr,turbo_mod,turbo_attr",
    DOOR_CRITICAL,
    ids=[f"{m}.{a}" for m, a, _, _ in DOOR_CRITICAL],
)
def test_door_critical_identity(real_mod, attr, turbo_mod, turbo_attr):
    """Plain-import name IS the turbo-registered object (single identity)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", UserWarning)
        real = importlib.import_module(real_mod)
        turbo = importlib.import_module(turbo_mod)
    real_obj = getattr(real, attr, None)
    turbo_obj = getattr(turbo, turbo_attr, None)
    assert real_obj is not None, f"{real_mod}.{attr} missing"
    assert turbo_obj is not None, f"{turbo_mod}.{turbo_attr} missing"
    assert real_obj is turbo_obj, (
        f"{real_mod}.{attr} is {real_obj!r} but the engine registered "
        f"{turbo_mod}.{turbo_attr} = {turbo_obj!r} — shim identity split"
    )


def test_plain_import_app_has_the_door():
    """``from fastapi import FastAPI`` yields an app exposing ``app.run()``."""
    from fastapi import FastAPI

    app = FastAPI()
    assert callable(getattr(app, "run", None)), (
        "plain `from fastapi import FastAPI` did not produce the turbo class "
        "(no .run door entry point)"
    )
