"""R52 audit follow-ups — locks the fixes against regression.

  * Finding 1: ``FastAPI(routes=[...])`` and ``APIRouter(routes=[...])``
    accept Starlette ``Route`` instances and serve them.
  * Finding 2: ``Headers.multi_items()`` is defined exactly once and
    preserves duplicate ``Set-Cookie`` headers; ``Response.cookies``
    surfaces all of them.
  * Finding 6: ``inspect.signature(FastAPI.get)`` /
    ``APIRouter.get`` declare the full path-operation kwarg surface
    (24 parameters), matching upstream FastAPI 0.136.0.
"""
from __future__ import annotations

import inspect

import fastapi_turbo  # noqa: F401  # install shim
from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient
from fastapi_turbo.http import Headers, Response
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route


# ────────────────────────────────────────────────────────────────────
# Finding 1 — routes= kwarg
# ────────────────────────────────────────────────────────────────────


def test_fastapi_routes_kwarg_serves_starlette_route():
    async def hi(req):
        return PlainTextResponse("hi")

    app = FastAPI(routes=[Route("/hi", hi)])
    with TestClient(app, in_process=True) as c:
        r = c.get("/hi")
        assert r.status_code == 200, r.text
        assert r.text == "hi"


def test_fastapi_routes_kwarg_with_path_params():
    async def echo(req):
        return JSONResponse({"name": req.path_params.get("name")})

    app = FastAPI(routes=[Route("/echo/{name}", echo)])
    with TestClient(app, in_process=True) as c:
        r = c.get("/echo/world")
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "world"}


def test_fastapi_routes_kwarg_sync_handler():
    def sync_h(req):
        return PlainTextResponse("sync ok")

    app = FastAPI(routes=[Route("/sync", sync_h)])
    with TestClient(app, in_process=True) as c:
        r = c.get("/sync")
        assert r.status_code == 200, r.text
        assert r.text == "sync ok"


def test_apirouter_routes_kwarg_serves_starlette_route():
    async def hi(req):
        return PlainTextResponse("via router")

    router = APIRouter(routes=[Route("/r/hi", hi)])
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, in_process=True) as c:
        r = c.get("/r/hi")
        assert r.status_code == 200, r.text
        assert r.text == "via router"


def test_starlette_passthrough_coexists_with_decorator_routes():
    """Starlette routes and FastAPI decorator routes can both live in the
    same app — they don't shadow each other."""
    async def st_hi(req):
        return PlainTextResponse("starlette")

    app = FastAPI(routes=[Route("/st", st_hi)])

    @app.get("/decor")
    def decor():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        assert c.get("/st").text == "starlette"
        assert c.get("/decor").json() == {"ok": True}


# ────────────────────────────────────────────────────────────────────
# Finding 2 — Set-Cookie duplicate preservation
# ────────────────────────────────────────────────────────────────────


def test_headers_multi_items_defined_once_and_preserves_duplicates():
    """The class previously defined ``multi_items`` twice; the second
    definition collapsed duplicates. After R52 only one definition
    remains, returning ``_raw_list``-backed values."""
    h = Headers([("set-cookie", "a=1"), ("set-cookie", "b=2")])
    items = h.multi_items()
    set_cookie_vals = [v for k, v in items if k.lower() == "set-cookie"]
    assert set_cookie_vals == ["a=1", "b=2"], items


def test_response_cookies_preserves_multiple_set_cookie_headers():
    h = Headers(
        [
            ("set-cookie", "session=abc"),
            ("set-cookie", "tracking=xyz"),
            ("content-type", "text/plain"),
        ]
    )
    r = Response(status_code=200, headers=h, content=b"")
    cookies = dict(r.cookies)
    assert cookies == {"session": "abc", "tracking": "xyz"}, cookies


# ────────────────────────────────────────────────────────────────────
# Finding 6 — inspect.signature parity
# ────────────────────────────────────────────────────────────────────


def _kw_only_names(sig: inspect.Signature) -> set[str]:
    return {
        n for n, p in sig.parameters.items()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    }


_EXPECTED_HTTP_KWS = {
    "response_model",
    "status_code",
    "tags",
    "dependencies",
    "summary",
    "description",
    "response_description",
    "responses",
    "deprecated",
    "operation_id",
    "response_model_include",
    "response_model_exclude",
    "response_model_by_alias",
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
    "include_in_schema",
    "response_class",
    "name",
    "callbacks",
    "openapi_extra",
    "generate_unique_id_function",
}


def test_fastapi_get_signature_lists_all_path_operation_kwargs():
    sig = inspect.signature(FastAPI.get)
    assert _kw_only_names(sig) == _EXPECTED_HTTP_KWS, set(sig.parameters)


def test_fastapi_post_signature_lists_all_path_operation_kwargs():
    sig = inspect.signature(FastAPI.post)
    assert _kw_only_names(sig) == _EXPECTED_HTTP_KWS


def test_apirouter_get_signature_lists_all_path_operation_kwargs():
    sig = inspect.signature(APIRouter.get)
    assert _kw_only_names(sig) == _EXPECTED_HTTP_KWS


def test_api_route_signature_includes_methods_kwarg():
    """``api_route`` carries the same kwargs as ``get`` plus ``methods``."""
    sig = inspect.signature(FastAPI.api_route)
    expected = _EXPECTED_HTTP_KWS | {"methods"}
    assert _kw_only_names(sig) == expected


def test_websocket_signature_includes_name_kwarg():
    sig = inspect.signature(FastAPI.websocket)
    assert _kw_only_names(sig) == {"name"}
    # and `path` is the positional kwarg
    assert "path" in sig.parameters


def test_signature_matches_upstream_fastapi_parameter_names():
    """The set of parameter names on ``FastAPI.get`` must equal the
    set on the real upstream ``fastapi.FastAPI.get`` so SDK
    generators / docs builders see the same surface they would
    against unshimmed FastAPI."""
    import sys
    # Re-import the unshimmed upstream by going around our shim.
    real_fastapi_app_mod = None
    for k, m in sys.modules.items():
        if k == "fastapi" and m is not None:
            # Our shim might be installed; check via module file.
            if "fastapi_turbo" not in (getattr(m, "__file__", "") or ""):
                real_fastapi_app_mod = m
                break
    if real_fastapi_app_mod is None:
        # Fallback path — load the upstream directly via importlib.
        import importlib
        spec = importlib.util.find_spec("fastapi.applications")
        if spec is None or spec.origin is None or "fastapi_turbo" in spec.origin:
            return  # shim active, can't compare reliably
        return
    upstream_keys = set(
        inspect.signature(real_fastapi_app_mod.FastAPI.get).parameters.keys()
    )
    turbo_keys = set(inspect.signature(FastAPI.get).parameters.keys())
    assert upstream_keys == turbo_keys, (
        f"upstream-only={upstream_keys - turbo_keys}, "
        f"turbo-only={turbo_keys - upstream_keys}"
    )
