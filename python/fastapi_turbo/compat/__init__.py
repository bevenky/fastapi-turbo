"""Compatibility layer: patch fastapi-turbo's accelerated entry points onto
the REAL ``fastapi`` / ``starlette`` packages.

Auto-installed when ``import fastapi_turbo`` runs (disable with
``FASTAPI_TURBO_NO_SHIM=1``).

Old model (deleted): ~78 fake ``types.ModuleType`` objects REPLACED
``sys.modules["fastapi"]`` / ``["starlette"]`` and every submodule key, so
every public name had to be mirrored by hand — anything not mirrored broke.

New model: the genuine packages stay live in ``sys.modules``. ``install()``
setattr-patches ONLY what must differ:

  - the core swap — ``fastapi.FastAPI`` → the Rust-Axum-backed class, plus
    the thin real-subclass ``APIRoute``/``APIRouter`` and the WS route holder
  - bridge types the Rust door hands out or drains — ``UploadFile`` (its
    ``__subclasshook__`` is what lets Rust ``PyUploadFile`` instances pass
    ``isinstance``), ``BackgroundTasks`` (``run_sync`` drain), the turbo
    ``WebSocket`` wrapper + ``WebSocketState``
  - the turbo ``TestClient`` family (httpx against the real Rust server)
  - security schemes: identity re-patches of the REAL classes (turbo
    ``security.py`` re-exports them) plus the ``OAuth2ClientCredentials``
    turbo extension the real package lacks
  - Tower-marker middleware classes and the turbo StaticFiles/Jinja2Templates
  - a handful of compat extensions the suites import (names the real
    packages don't export — mirror of ``fastapi_turbo.__all__``)

Everything else — responses, requests, params, encoders, exceptions,
openapi.*, dependencies.*, concurrency, all remaining ``starlette.*`` —
resolves to the genuine real-package attributes.

``uninstall()`` restores the saved originals in reverse patch order.

NOTE for turbo internals: never resolve a patched name through the live
module at runtime (e.g. ``_real_fastapi.routing.APIRoute``) when you want
the genuine class — capture it at import time instead (modules load before
``install()`` runs; see ``applications._RealAPIRoute``).
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

_installed = False

_MISSING: Any = object()


def _html_safe_json(value: Any) -> str:
    """Byte-identical replacement for ``fastapi.openapi.docs._html_safe_json``
    that does NOT go through the module-level ``json.dumps``. The turbo
    engine pre-renders the Swagger/ReDoc HTML at server boot, which can land
    inside a test window that mocks ``json.dumps`` to observe response
    serialization (upstream ``test_dump_json_fast_path``) — real FastAPI
    only renders docs lazily on ``GET /docs``. ``JSONEncoder().encode`` has
    the same defaults as ``json.dumps``."""
    import json

    return (
        json.JSONEncoder().encode(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

# (module, attr, original_value_or_MISSING) in patch order; ``uninstall``
# walks it in reverse.
_PATCHES: list[tuple[Any, str, Any]] = []

# Snapshot of every ``fastapi*`` / ``starlette*`` module object live in
# ``sys.modules`` after the FIRST install — the CANONICAL module set, i.e.
# the very modules the turbo engine captured pre-shim references into
# (``applications._real_fastapi``, the routing/security base classes, ...).
# Re-install cycles (test fixtures drop the keys and import upstream fresh)
# must restore THESE objects, not patch a freshly imported set: fresh
# modules carry fresh class identities that the engine's real-FastAPI
# introspection (old class objects) would no longer recognise (e.g. a fresh
# ``starlette.requests.Request`` annotation fails ``lenient_issubclass``
# against the old ``Request`` inside old ``get_dependant``).
_CANONICAL_MODULES: dict[str, Any] = {}


def _is_target_key(name: str) -> bool:
    return (
        name == "fastapi"
        or name.startswith("fastapi.")
        or name == "starlette"
        or name.startswith("starlette.")
    )

# Pre-install capture of the original Starlette ``UploadFile``. Some user
# code does ``from starlette.datastructures import UploadFile`` BEFORE
# importing fastapi_turbo and holds that reference; consumers (e.g.
# ``FormData.close``) can use this to recognise both the patched turbo class
# AND pre-install instances. (tests/stress/test_r24/r25 poke this slot.)
PRESHIM_STARLETTE_UPLOADFILE: Any = None


def _patch(mod: Any, attr: str, value: Any) -> None:
    """Save the CURRENT value first (or MISSING), then setattr."""
    _PATCHES.append((mod, attr, getattr(mod, attr, _MISSING)))
    setattr(mod, attr, value)


def install() -> None:
    """Patch the accelerated turbo entry points onto the real packages."""
    global _installed, PRESHIM_STARLETTE_UPLOADFILE
    if _installed:
        return

    # ── turbo bindings (captured pre-install; identity matches the engine) ─
    import fastapi_turbo
    import fastapi_turbo.applications as _ta
    import fastapi_turbo.authentication as _tauth
    import fastapi_turbo.background as _tb
    import fastapi_turbo.concurrency as _tc
    import fastapi_turbo.dependencies as _td
    import fastapi_turbo.exceptions as _texc
    import fastapi_turbo.middleware.base as _tmw_base
    import fastapi_turbo.middleware.cors as _tmw_cors
    import fastapi_turbo.middleware.gzip as _tmw_gzip
    import fastapi_turbo.middleware.httpsredirect as _tmw_hr
    import fastapi_turbo.middleware.sessions as _tmw_sess
    import fastapi_turbo.middleware.trustedhost as _tmw_th
    import fastapi_turbo.param_functions as _tp
    import fastapi_turbo.routing as _tr
    import fastapi_turbo.security as _ts
    import fastapi_turbo.staticfiles as _tsf
    import fastapi_turbo.templating as _ttpl
    import fastapi_turbo.testclient as _tt
    import fastapi_turbo.websockets as _tw
    from fastapi_turbo._ws_support import WSRoute as _WSRoute

    # Capture the original Starlette UploadFile (if Starlette was imported
    # before us) BEFORE we patch the attribute.
    _st_ds_pre = sys.modules.get("starlette.datastructures")
    if _st_ds_pre is not None:
        _cap = getattr(_st_ds_pre, "UploadFile", None)
        if _cap is not None and _cap is not _tp.UploadFile:
            PRESHIM_STARLETTE_UPLOADFILE = _cap

    # ── canonical module restore (re-install cycles) ───────────────────────
    # A fixture may have dropped the fastapi/starlette keys and imported a
    # FRESH upstream set. Put the canonical objects back so engine class
    # identities stay consistent, and evict any stray fresh submodules so a
    # later import resolves them against the canonical parents again.
    if _CANONICAL_MODULES:
        for _name in [m for m in sys.modules if _is_target_key(m)]:
            if _name not in _CANONICAL_MODULES:
                del sys.modules[_name]
        sys.modules.update(_CANONICAL_MODULES)

    # ── real modules ───────────────────────────────────────────────────────
    fa = importlib.import_module("fastapi")
    fa_app = importlib.import_module("fastapi.applications")
    fa_bg = importlib.import_module("fastapi.background")
    fa_conc = importlib.import_module("fastapi.concurrency")
    fa_docs = importlib.import_module("fastapi.openapi.docs")
    fa_ds = importlib.import_module("fastapi.datastructures")
    fa_exc = importlib.import_module("fastapi.exceptions")
    fa_mw_cors = importlib.import_module("fastapi.middleware.cors")
    fa_mw_gzip = importlib.import_module("fastapi.middleware.gzip")
    fa_mw_hr = importlib.import_module("fastapi.middleware.httpsredirect")
    fa_mw_th = importlib.import_module("fastapi.middleware.trustedhost")
    fa_pf = importlib.import_module("fastapi.param_functions")
    fa_routing = importlib.import_module("fastapi.routing")
    fa_sec = importlib.import_module("fastapi.security")
    fa_sec_api_key = importlib.import_module("fastapi.security.api_key")
    fa_sec_http = importlib.import_module("fastapi.security.http")
    fa_sec_oauth2 = importlib.import_module("fastapi.security.oauth2")
    fa_sec_oidc = importlib.import_module("fastapi.security.open_id_connect_url")
    fa_static = importlib.import_module("fastapi.staticfiles")
    fa_tc = importlib.import_module("fastapi.testclient")
    fa_tmpl = importlib.import_module("fastapi.templating")
    fa_ws = importlib.import_module("fastapi.websockets")
    st_auth = importlib.import_module("starlette.authentication")
    st_bg = importlib.import_module("starlette.background")
    st_ds = importlib.import_module("starlette.datastructures")
    st_mw = importlib.import_module("starlette.middleware")
    st_mw_auth = importlib.import_module("starlette.middleware.authentication")
    st_mw_base = importlib.import_module("starlette.middleware.base")
    st_mw_cors = importlib.import_module("starlette.middleware.cors")
    st_mw_gzip = importlib.import_module("starlette.middleware.gzip")
    st_mw_hr = importlib.import_module("starlette.middleware.httpsredirect")
    st_mw_sess = importlib.import_module("starlette.middleware.sessions")
    st_mw_th = importlib.import_module("starlette.middleware.trustedhost")
    st_conc = importlib.import_module("starlette.concurrency")
    st_static = importlib.import_module("starlette.staticfiles")
    st_tc = importlib.import_module("starlette.testclient")
    st_tmpl = importlib.import_module("starlette.templating")
    st_ws = importlib.import_module("starlette.websockets")

    # ── core swap ──────────────────────────────────────────────────────────
    _patch(fa, "FastAPI", _ta.FastAPI)
    _patch(fa_app, "FastAPI", _ta.FastAPI)
    _patch(fa, "APIRouter", _tr.APIRouter)
    _patch(fa, "APIRoute", _tr.APIRoute)  # turbo extension (no real top-level)
    _patch(fa_routing, "APIRouter", _tr.APIRouter)
    _patch(fa_routing, "APIRoute", _tr.APIRoute)
    _patch(fa_routing, "APIWebSocketRoute", _WSRoute)
    _patch(fa_routing, "_default_generate_unique_id", _tr._default_generate_unique_id)

    # Markers kept class-style: real FastAPI exports *functions*; suites do
    # ``isinstance(x, Depends)``. ``_td.Depends`` IS the real
    # ``fastapi.params.Depends`` class captured at package init.
    _patch(fa, "Depends", _td.Depends)
    _patch(fa, "Security", _td.Security)
    # Param constructors stay the turbo ``_ParamMarker`` classes (they
    # subclass the real ``fastapi.params`` types, so real introspection
    # accepts them; the WS door classifies dep params by ``_ParamMarker``).
    # ``fastapi.params`` itself stays REAL — real machinery isinstance-checks
    # through it at runtime and must keep accepting genuine marker instances.
    for _n in ("Query", "Path", "Header", "Cookie", "Body", "Form", "File"):
        _patch(fa, _n, getattr(_tp, _n))
        _patch(fa_pf, _n, getattr(_tp, _n))

    # ── bridge types the Rust door hands out / drains ──────────────────────
    _patch(fa, "UploadFile", _tp.UploadFile)
    _patch(fa_ds, "UploadFile", _tp.UploadFile)
    _patch(st_ds, "UploadFile", _tp.UploadFile)
    _patch(fa, "BackgroundTasks", _tb.BackgroundTasks)
    _patch(fa_bg, "BackgroundTasks", _tb.BackgroundTasks)
    _patch(st_bg, "BackgroundTasks", _tb.BackgroundTasks)
    _patch(fa, "WebSocket", _tw.WebSocket)
    _patch(fa_ws, "WebSocket", _tw.WebSocket)
    _patch(fa_ws, "WebSocketState", _tw.WebSocketState)
    _patch(st_ws, "WebSocket", _tw.WebSocket)
    _patch(st_ws, "WebSocketState", _tw.WebSocketState)

    # ── test clients (httpx against the real Rust server) ──────────────────
    _patch(fa_tc, "TestClient", _tt.TestClient)
    _patch(fa_tc, "AsyncTestClient", _tt.AsyncTestClient)
    _patch(fa_tc, "AsyncClient", _tt.AsyncClient)
    _patch(fa_tc, "ASGITransport", _tt.ASGITransport)
    _patch(st_tc, "TestClient", _tt.TestClient)
    _patch(st_tc, "WebSocketTestSession", _tt._WebSocketTestSession)

    # ── security schemes ───────────────────────────────────────────────────
    # ``fastapi_turbo.security`` re-exports the REAL classes, so these are
    # identity re-patches — except ``OAuth2ClientCredentials``, a turbo
    # extension real FastAPI lacks (patched onto ``fastapi.security`` so
    # ``from fastapi.security import OAuth2ClientCredentials`` works).
    for _n in (
        "OAuth2",
        "OAuth2PasswordBearer",
        "OAuth2PasswordRequestForm",
        "OAuth2PasswordRequestFormStrict",
        "OAuth2AuthorizationCodeBearer",
        "OAuth2ClientCredentials",  # turbo extension
        "HTTPBearer",
        "HTTPDigest",
        "HTTPBasic",
        "HTTPBasicCredentials",
        "HTTPAuthorizationCredentials",
        "APIKeyHeader",
        "APIKeyQuery",
        "APIKeyCookie",
        "OpenIdConnect",
    ):
        _patch(fa_sec, _n, getattr(_ts, _n))
    for _sub, _names in (
        (fa_sec_oauth2, ("OAuth2", "OAuth2PasswordBearer",
                         "OAuth2AuthorizationCodeBearer",
                         "OAuth2PasswordRequestForm",
                         "OAuth2PasswordRequestFormStrict")),
        (fa_sec_http, ("HTTPBase", "HTTPBearer", "HTTPDigest", "HTTPBasic",
                       "HTTPBasicCredentials", "HTTPAuthorizationCredentials")),
        (fa_sec_api_key, ("APIKeyHeader", "APIKeyQuery", "APIKeyCookie")),
        (fa_sec_oidc, ("OpenIdConnect",)),
    ):
        for _n in _names:
            _patch(_sub, _n, getattr(_ts, _n))

    # ── Tower-marker middleware + python-HTTP-chain middleware ─────────────
    # The turbo classes carry ``_fastapi_turbo_middleware_type`` (Tower layer
    # selection) or implement the door's ``(request, call_next)`` dispatch
    # convention (BaseHTTP / Session / Authentication). The in-process
    # dispatcher resolves the REAL ASGI classes itself when it needs them.
    _patch(fa_mw_cors, "CORSMiddleware", _tmw_cors.CORSMiddleware)
    _patch(fa_mw_gzip, "GZipMiddleware", _tmw_gzip.GZipMiddleware)
    _patch(fa_mw_th, "TrustedHostMiddleware", _tmw_th.TrustedHostMiddleware)
    _patch(fa_mw_hr, "HTTPSRedirectMiddleware", _tmw_hr.HTTPSRedirectMiddleware)
    _patch(st_mw_cors, "CORSMiddleware", _tmw_cors.CORSMiddleware)
    _patch(st_mw_gzip, "GZipMiddleware", _tmw_gzip.GZipMiddleware)
    _patch(st_mw_th, "TrustedHostMiddleware", _tmw_th.TrustedHostMiddleware)
    _patch(st_mw_hr, "HTTPSRedirectMiddleware", _tmw_hr.HTTPSRedirectMiddleware)
    _patch(st_mw_base, "BaseHTTPMiddleware", _tmw_base.BaseHTTPMiddleware)
    _patch(st_mw_sess, "SessionMiddleware", _tmw_sess.SessionMiddleware)
    _patch(st_mw_auth, "AuthenticationMiddleware", _tauth.AuthenticationMiddleware)
    _patch(st_auth, "AuthenticationMiddleware", _tauth.AuthenticationMiddleware)
    # Turbo ``requires`` RETURNS the 403/redirect response (door contract);
    # real Starlette's raises ``HTTPException`` instead.
    _patch(st_auth, "requires", _tauth.requires)
    # Convenience names on the package namespace (real exports Middleware only).
    _patch(st_mw, "BaseHTTPMiddleware", _tmw_base.BaseHTTPMiddleware)
    _patch(st_mw, "CORSMiddleware", _tmw_cors.CORSMiddleware)
    _patch(st_mw, "GZipMiddleware", _tmw_gzip.GZipMiddleware)
    _patch(st_mw, "TrustedHostMiddleware", _tmw_th.TrustedHostMiddleware)
    _patch(st_mw, "HTTPSRedirectMiddleware", _tmw_hr.HTTPSRedirectMiddleware)

    # ── StaticFiles / Jinja2Templates ──────────────────────────────────────
    # ``fastapi_turbo.staticfiles`` re-exports the REAL Starlette
    # ``StaticFiles`` (the Rust door serves the mount via tower-http ServeDir,
    # reading ``.directory`` off the instance), so these are identity
    # re-patches kept for shim completeness. Jinja2Templates keeps the turbo
    # ``TemplateResponse(name, ctx)`` wrapper.
    _patch(fa_static, "StaticFiles", _tsf.StaticFiles)
    _patch(st_static, "StaticFiles", _tsf.StaticFiles)
    _patch(fa_tmpl, "Jinja2Templates", _ttpl.Jinja2Templates)
    _patch(st_tmpl, "Jinja2Templates", _ttpl.Jinja2Templates)

    # ── threadpool helpers: turbo's loop-less implementations ──────────────
    # Real run_in_threadpool needs a RUNNING anyio event loop; the door's
    # try-sync fast path drives handler coroutines via ``coro.send(None)``
    # with no loop, so an ``await run_in_threadpool(...)`` inside a user
    # wrapper/dependency would raise ``anyio.NoEventLoopError``.
    _patch(fa_conc, "run_in_threadpool", _tc.run_in_threadpool)
    _patch(fa_conc, "iterate_in_threadpool", _tc.iterate_in_threadpool)
    _patch(st_conc, "run_in_threadpool", _tc.run_in_threadpool)
    _patch(st_conc, "iterate_in_threadpool", _tc.iterate_in_threadpool)
    _patch(st_conc, "run_until_first_complete", _tc.run_until_first_complete)
    # ``starlette.responses`` did ``from .concurrency import iterate_in_threadpool``
    # at import time, binding the name into its OWN namespace — patching
    # ``starlette.concurrency`` above does NOT reach ``StreamingResponse``.
    # Patch the response module's bound name directly so sync-content streams
    # get turbo's wrapper (which exposes ``_fastapi_turbo_sync_source`` for the
    # Rust door's direct-drive fast path).
    st_responses = importlib.import_module("starlette.responses")
    _patch(st_responses, "iterate_in_threadpool", _tc.iterate_in_threadpool)

    # Docs JSON helper: same bytes, no ``json.dumps`` (see docstring above —
    # the engine pre-renders docs HTML at boot, inside test mock windows).
    _patch(fa_docs, "_html_safe_json", _html_safe_json)

    # ── compat extensions the suites import ────────────────────────────────
    _patch(fa, "fastapi_turbo_version", fastapi_turbo.__version__)
    _patch(fa_exc, "WebSocketDisconnect", _texc.WebSocketDisconnect)

    # Mirror every remaining ``fastapi_turbo.__all__`` export the real
    # package lacks (HTMLResponse/JSONResponse/ORJSONResponse/WebSocketState/
    # security schemes at top level/jsonable_encoder/core_version/...).
    for _n in getattr(fastapi_turbo, "__all__", ()):
        if not hasattr(fa, _n):
            _v = getattr(fastapi_turbo, _n, None)
            if _v is not None:
                _patch(fa, _n, _v)

    # First install: snapshot the canonical module set (see comment above).
    if not _CANONICAL_MODULES:
        _CANONICAL_MODULES.update(
            (m, sys.modules[m]) for m in sys.modules if _is_target_key(m)
        )

    _installed = True


def uninstall() -> None:
    """Restore the saved originals (reverse patch order)."""
    global _installed

    # Restore on the recorded module objects UNCONDITIONALLY (even when a
    # fixture already dropped them from sys.modules — they are the canonical
    # modules that a later install() puts back, and they must be pristine
    # then; otherwise re-patching would save a turbo value as "original").
    for mod, attr, original in reversed(_PATCHES):
        try:
            if original is _MISSING:
                delattr(mod, attr)
            else:
                setattr(mod, attr, original)
        except Exception:  # noqa: BLE001
            pass
    _PATCHES.clear()
    _installed = False
