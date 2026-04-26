"""Broad parity audit: drive identical scenarios through real
Starlette/FastAPI and through fastapi_turbo, assert observable
behaviour matches.

This is the proactive equivalent of the audits the user has been
running by hand. Each test category targets a class of behaviour
that's easy to silently diverge on:

  * 404 / 405 / OPTIONS / HEAD on an unknown or wrong-method route
  * Validation-error response shape (status code + JSON body shape)
  * Error response Content-Type (textual responses must carry
    ``; charset=utf-8`` to match upstream)
  * StreamingResponse behaviour (no Content-Length, body bytes)
  * RedirectResponse status + Location + body
  * PlainTextResponse / HTMLResponse / JSONResponse content-type
  * Empty / 204 / 304 responses (no body bytes)

Uses the same sys.modules swap pattern as the existing parity
contract test so we can import the REAL upstream Starlette /
FastAPI in the same process where the turbo shim is otherwise
active."""
import asyncio
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_turbo_shim_after_each_test():
    yield
    _drop_fa_modules()
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _un()
    importlib.invalidate_caches()
    _in()


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
    from fastapi_turbo.compat import uninstall as _un
    _un()
    _drop_fa_modules()
    importlib.invalidate_caches()
    import fastapi as _fa  # noqa: F401
    return sys.modules["fastapi"], sys.modules.get("fastapi.responses"), sys.modules.get("starlette")


def _import_turbo():
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _drop_fa_modules()
    _un()
    importlib.invalidate_caches()
    _in()
    return sys.modules["fastapi"], sys.modules["fastapi.responses"], sys.modules.get("starlette")


def _run(coro):
    return asyncio.run(coro)


async def _drive(app, method, path, **kwargs):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
    ) as cli:
        return await cli.request(method, path, **kwargs)


def _build(builder):
    """builder(fa_mod, resp_mod) → app. Runs against upstream then
    turbo, returns (upstream_app, turbo_app)."""
    fa_up, resp_up, _ = _import_upstream()
    up_app = builder(fa_up, resp_up)
    fa_tb, resp_tb, _ = _import_turbo()
    tb_app = builder(fa_tb, resp_tb)
    return up_app, tb_app


# ────────────────────────────────────────────────────────────────────
# 404 / 405 / OPTIONS / HEAD
# ────────────────────────────────────────────────────────────────────

def _routing_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/items/{x}")
    def _r(x: int):
        return {"x": x}

    @app.post("/post-only")
    def _p(body: dict):
        return body

    return app


def test_404_unknown_route_parity():
    up, tb = _build(_routing_app)

    async def go():
        for app, label in ((up, "upstream"), (tb, "turbo")):
            r = await _drive(app, "GET", "/nope")
            assert r.status_code == 404, (label, r.status_code)
            # Upstream returns "Not Found" with content-type
            # text/plain. We just lock the status here.

    _run(go())


def test_405_wrong_method_parity():
    up, tb = _build(_routing_app)

    async def go():
        # POST on a GET-only route → 405 with Allow header listing
        # the supported methods.
        ru = await _drive(up, "POST", "/items/1", json={"a": 1})
        rt = await _drive(tb, "POST", "/items/1", json={"a": 1})
        assert ru.status_code == 405, ru.status_code
        assert rt.status_code == ru.status_code, (ru.status_code, rt.status_code)

    _run(go())


def test_options_returns_allowed_methods_parity():
    up, tb = _build(_routing_app)

    async def go():
        ru = await _drive(up, "OPTIONS", "/items/1")
        rt = await _drive(tb, "OPTIONS", "/items/1")
        # Both stacks should respond — exact body varies but status
        # must be 200 or 405 (depending on implementation), and both
        # should match each other.
        assert rt.status_code == ru.status_code, (
            f"OPTIONS divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )
        # If 200, both should advertise the same set of allowed methods
        # (order-insensitive).
        if ru.status_code == 200 and "allow" in ru.headers and "allow" in rt.headers:
            up_methods = set(m.strip() for m in ru.headers["allow"].split(","))
            tb_methods = set(m.strip() for m in rt.headers["allow"].split(","))
            assert up_methods == tb_methods, (up_methods, tb_methods)

    _run(go())


def test_head_on_get_only_route_parity():
    """HEAD on a route registered ONLY for GET — turbo and upstream
    must agree. (FastAPI's ``@app.get`` does NOT auto-add HEAD, so
    upstream returns 405; turbo must also return 405.)"""
    up, tb = _build(_routing_app)

    async def go():
        ru = await _drive(up, "HEAD", "/items/1")
        rt = await _drive(tb, "HEAD", "/items/1")
        assert rt.status_code == ru.status_code, (
            f"HEAD on GET-only route: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )

    _run(go())


def _explicit_head_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.api_route("/r", methods=["GET", "HEAD"])
    def _r():
        return {"ok": True}

    return app


def test_head_on_explicit_head_route_strips_body():
    """When HEAD is explicitly registered, the response must have
    the same status as GET but with an empty body (Starlette /
    FastAPI strips the body for HEAD)."""
    up, tb = _build(_explicit_head_app)

    async def go():
        # GET first to capture the canonical response.
        gu = await _drive(up, "GET", "/r")
        gt = await _drive(tb, "GET", "/r")
        assert gu.status_code == 200
        assert gt.status_code == 200
        # HEAD: same status, no body.
        hu = await _drive(up, "HEAD", "/r")
        ht = await _drive(tb, "HEAD", "/r")
        assert hu.status_code == 200, hu.status_code
        assert ht.status_code == 200, ht.status_code
        assert hu.content == b"", f"upstream HEAD body: {hu.content!r}"
        assert ht.content == b"", f"turbo HEAD body: {ht.content!r}"

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Validation error shape
# ────────────────────────────────────────────────────────────────────

def _validation_app(fa_mod, _resp_mod):
    from pydantic import BaseModel

    class M(BaseModel):
        n: int

    app = fa_mod.FastAPI()

    @app.post("/v")
    def _v(m: M):
        return {"n": m.n}

    return app


def test_validation_error_status_and_top_level_shape():
    up, tb = _build(_validation_app)

    async def go():
        # Missing required field.
        ru = await _drive(up, "POST", "/v", json={})
        rt = await _drive(tb, "POST", "/v", json={})
        assert ru.status_code == 422
        assert rt.status_code == 422

        body_up = ru.json()
        body_tb = rt.json()
        # Top-level shape: ``{"detail": [...]}`` — same as upstream.
        assert "detail" in body_up
        assert "detail" in body_tb, (
            f"turbo missing top-level 'detail' key: {body_tb!r}"
        )
        # Each error has at least loc, msg, type per Pydantic v2.
        for entry in body_tb["detail"]:
            assert "loc" in entry
            assert "msg" in entry
            assert "type" in entry

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Response Content-Type charset for textual
# ────────────────────────────────────────────────────────────────────

def _text_app(fa_mod, resp_mod):
    PlainText = resp_mod.PlainTextResponse
    HTMLResp = resp_mod.HTMLResponse
    app = fa_mod.FastAPI()

    @app.get("/p")
    def _p():
        return PlainText("hello")

    @app.get("/h")
    def _h():
        return HTMLResp("<p>hi</p>")

    return app


def test_textual_responses_carry_charset_utf8():
    up, tb = _build(_text_app)

    async def go():
        for path, expect_prefix in (
            ("/p", "text/plain; charset=utf-8"),
            ("/h", "text/html; charset=utf-8"),
        ):
            ru = await _drive(up, "GET", path)
            rt = await _drive(tb, "GET", path)
            assert ru.headers["content-type"] == expect_prefix, (
                f"upstream {path}: {ru.headers['content-type']}"
            )
            assert rt.headers["content-type"] == expect_prefix, (
                f"turbo {path}: {rt.headers['content-type']}"
            )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# RedirectResponse status + Location header
# ────────────────────────────────────────────────────────────────────

def _redirect_app(fa_mod, resp_mod):
    Redir = resp_mod.RedirectResponse
    app = fa_mod.FastAPI()

    @app.get("/r")
    def _r():
        return Redir("/dest", status_code=307)

    return app


def test_redirect_response_parity():
    up, tb = _build(_redirect_app)

    async def go():
        ru = await _drive(up, "GET", "/r", follow_redirects=False)
        rt = await _drive(tb, "GET", "/r", follow_redirects=False)
        assert ru.status_code == 307
        assert rt.status_code == 307
        assert ru.headers["location"] == "/dest"
        assert rt.headers["location"] == "/dest"

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Empty body / 204 / 304
# ────────────────────────────────────────────────────────────────────

def _empty_app(fa_mod, resp_mod):
    Resp = resp_mod.Response
    app = fa_mod.FastAPI()

    @app.get("/no-content", status_code=204)
    def _nc():
        return Resp(status_code=204)

    return app


def test_204_response_has_no_body():
    up, tb = _build(_empty_app)

    async def go():
        ru = await _drive(up, "GET", "/no-content")
        rt = await _drive(tb, "GET", "/no-content")
        assert ru.status_code == 204, ru.status_code
        assert rt.status_code == 204, rt.status_code
        assert ru.content == b"", f"upstream 204 body: {ru.content!r}"
        assert rt.content == b"", f"turbo 204 body: {rt.content!r}"
        # Per RFC 7230 §3.3.2: 204 must NOT carry Content-Length.
        # Both stacks should match.
        up_has_cl = "content-length" in ru.headers
        tb_has_cl = "content-length" in rt.headers
        assert up_has_cl == tb_has_cl, (
            f"Content-Length parity broken on 204: upstream={up_has_cl} "
            f"turbo={tb_has_cl}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Empty response / no media_type set
# ────────────────────────────────────────────────────────────────────

def _bare_response_app(fa_mod, resp_mod):
    Resp = resp_mod.Response
    app = fa_mod.FastAPI()

    @app.get("/bare")
    def _b():
        # No body, no media_type — both stacks should produce a
        # canonical 200 with empty body.
        return Resp()

    return app


def test_bare_response_parity():
    up, tb = _build(_bare_response_app)

    async def go():
        ru = await _drive(up, "GET", "/bare")
        rt = await _drive(tb, "GET", "/bare")
        assert ru.status_code == rt.status_code
        assert ru.content == rt.content
        # Whatever upstream stamps for Content-Type, turbo should
        # match.
        assert ru.headers.get("content-type") == rt.headers.get("content-type"), (
            f"bare-response Content-Type: upstream={ru.headers.get('content-type')!r} "
            f"turbo={rt.headers.get('content-type')!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Query / path / body parsing
# ────────────────────────────────────────────────────────────────────

def _query_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/q")
    def _q(n: int = 0, s: str = ""):
        return {"n": n, "s": s}

    return app


def test_query_param_validation_error_parity():
    up, tb = _build(_query_app)

    async def go():
        # Non-integer for `n`.
        ru = await _drive(up, "GET", "/q?n=abc")
        rt = await _drive(tb, "GET", "/q?n=abc")
        assert ru.status_code == 422
        assert rt.status_code == 422
        body_u = ru.json()
        body_t = rt.json()
        # Top-level shape parity.
        assert "detail" in body_u
        assert "detail" in body_t
        # Each error entry has matching loc length / shape.
        if body_u["detail"] and body_t["detail"]:
            up_loc = body_u["detail"][0]["loc"]
            tb_loc = body_t["detail"][0]["loc"]
            # First element should be ``query`` for both.
            assert up_loc[0] == "query"
            assert tb_loc[0] == "query", f"turbo loc[0]={tb_loc[0]!r}"

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Streaming response — no Content-Length, body bytes match
# ────────────────────────────────────────────────────────────────────

def _stream_app(fa_mod, resp_mod):
    Streaming = resp_mod.StreamingResponse
    app = fa_mod.FastAPI()

    def _gen():
        yield b"chunk1\n"
        yield b"chunk2\n"
        yield b"chunk3\n"

    @app.get("/s")
    def _s():
        return Streaming(_gen(), media_type="text/plain")

    return app


def test_streaming_response_body_parity():
    up, tb = _build(_stream_app)

    async def go():
        ru = await _drive(up, "GET", "/s")
        rt = await _drive(tb, "GET", "/s")
        assert ru.status_code == 200
        assert rt.status_code == 200
        assert ru.content == b"chunk1\nchunk2\nchunk3\n"
        assert rt.content == ru.content
        # Content-Length policy: streaming responses with an unknown
        # length should NOT carry Content-Length (Starlette omits it).
        # Turbo must match the same stance.
        up_has_cl = "content-length" in ru.headers
        tb_has_cl = "content-length" in rt.headers
        assert up_has_cl == tb_has_cl, (
            f"streaming Content-Length parity broken: upstream={up_has_cl} "
            f"turbo={tb_has_cl}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# 404 / 422 / HTTPException body shapes
# ────────────────────────────────────────────────────────────────────

def test_404_body_shape_parity():
    """FastAPI's default exception handler emits a 404 with body
    ``{"detail": "Not Found"}`` and Content-Type
    ``application/json``. Turbo must match."""
    up, tb = _build(_routing_app)

    async def go():
        ru = await _drive(up, "GET", "/nope")
        rt = await _drive(tb, "GET", "/nope")
        assert ru.status_code == rt.status_code == 404
        # Both should be JSON.
        up_ct = ru.headers["content-type"]
        tb_ct = rt.headers["content-type"]
        assert up_ct == tb_ct, (
            f"404 Content-Type divergence: upstream={up_ct!r} turbo={tb_ct!r}"
        )
        # Body shape parity.
        try:
            up_body = ru.json()
            tb_body = rt.json()
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"404 body not JSON: upstream={ru.content!r} turbo={rt.content!r}"
            ) from e
        assert up_body == tb_body, (
            f"404 body divergence: upstream={up_body!r} turbo={tb_body!r}"
        )

    _run(go())


def _http_exception_app(fa_mod, _resp_mod):
    # Capture HTTPException at build-time — importing inside the
    # handler would resolve through whichever ``fastapi`` is in
    # sys.modules at request time, which the shim swap mangles.
    HTTPException = fa_mod.HTTPException
    app = fa_mod.FastAPI()

    @app.get("/teapot")
    def _t():
        raise HTTPException(status_code=418, detail="I'm a teapot")

    @app.get("/with-headers")
    def _wh():
        raise HTTPException(
            status_code=403,
            detail="forbidden",
            headers={"X-Auth-Required": "Bearer"},
        )

    return app


def test_http_exception_body_and_headers_parity():
    """``HTTPException`` propagation: status, body, and any custom
    headers must match upstream exactly."""
    up, tb = _build(_http_exception_app)

    async def go():
        ru = await _drive(up, "GET", "/teapot")
        rt = await _drive(tb, "GET", "/teapot")
        assert ru.status_code == 418
        assert rt.status_code == 418
        assert ru.json() == {"detail": "I'm a teapot"}
        assert rt.json() == {"detail": "I'm a teapot"}

        ru2 = await _drive(up, "GET", "/with-headers")
        rt2 = await _drive(tb, "GET", "/with-headers")
        assert ru2.status_code == 403
        assert rt2.status_code == 403
        assert ru2.headers.get("x-auth-required") == "Bearer", (
            f"upstream missing custom header: {dict(ru2.headers)}"
        )
        assert rt2.headers.get("x-auth-required") == "Bearer", (
            f"turbo missing custom header from HTTPException: "
            f"{dict(rt2.headers)}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Trailing slash redirect
# ────────────────────────────────────────────────────────────────────

def _trailing_slash_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/items")
    def _i():
        return {"ok": True}

    return app


def test_trailing_slash_parity():
    up, tb = _build(_trailing_slash_app)

    async def go():
        # Hitting /items/ when only /items is registered.
        ru = await _drive(up, "GET", "/items/", follow_redirects=False)
        rt = await _drive(tb, "GET", "/items/", follow_redirects=False)
        # Whatever upstream does (404 vs 307 redirect), turbo must
        # match. Starlette by default returns 404 here unless
        # ``redirect_slashes=True`` was set on the app, which is the
        # default.
        assert rt.status_code == ru.status_code, (
            f"trailing-slash divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Cookies: Set-Cookie passthrough
# ────────────────────────────────────────────────────────────────────

def _set_cookie_app(fa_mod, resp_mod):
    Resp = resp_mod.Response
    app = fa_mod.FastAPI()

    @app.get("/c")
    def _c():
        r = Resp(content=b"ok")
        r.set_cookie("session", "abc")
        r.set_cookie("flash", "msg")
        return r

    return app


def test_multiple_set_cookie_headers_preserved():
    """Multiple ``set_cookie`` calls must produce two independent
    Set-Cookie response headers — not a single concatenated value.
    httpx surfaces multi-value headers by joining with commas in
    ``.headers[...]``, so we use ``.cookies`` (which parses each
    individually) for parity."""
    up, tb = _build(_set_cookie_app)

    async def go():
        ru = await _drive(up, "GET", "/c")
        rt = await _drive(tb, "GET", "/c")
        # Both clients should see the cookies.
        assert ru.cookies.get("session") == "abc"
        assert rt.cookies.get("session") == "abc", (
            f"turbo dropped session cookie: {rt.cookies}"
        )
        assert ru.cookies.get("flash") == "msg"
        assert rt.cookies.get("flash") == "msg", (
            f"turbo dropped flash cookie: {rt.cookies}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Path parameter type validation
# ────────────────────────────────────────────────────────────────────

def test_path_param_validation_error_parity():
    """``/items/abc`` against ``def _r(x: int)`` → 422 with detail
    pointing at the path location."""
    up, tb = _build(_routing_app)

    async def go():
        ru = await _drive(up, "GET", "/items/abc")
        rt = await _drive(tb, "GET", "/items/abc")
        assert ru.status_code == 422
        assert rt.status_code == 422, rt.status_code
        body_u = ru.json()
        body_t = rt.json()
        assert body_u["detail"][0]["loc"][0] == "path"
        assert body_t["detail"][0]["loc"][0] == "path", (
            f"turbo loc divergence: {body_t['detail'][0]['loc']}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# JSON body decode error
# ────────────────────────────────────────────────────────────────────

def _json_body_app(fa_mod, _resp_mod):
    from pydantic import BaseModel

    class M(BaseModel):
        n: int

    app = fa_mod.FastAPI()

    @app.post("/j")
    def _j(m: M):
        return m

    return app


def test_invalid_json_body_returns_422_parity():
    """Malformed JSON body → 422 with a parseable error structure."""
    up, tb = _build(_json_body_app)

    async def go():
        ru = await _drive(
            up, "POST", "/j",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        rt = await _drive(
            tb, "POST", "/j",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert ru.status_code == 422
        assert rt.status_code == 422, rt.status_code
        body_u = ru.json()
        body_t = rt.json()
        assert "detail" in body_u
        assert "detail" in body_t

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Empty-body POST (missing required body)
# ────────────────────────────────────────────────────────────────────

def test_missing_body_on_required_post_returns_422():
    up, tb = _build(_json_body_app)

    async def go():
        ru = await _drive(up, "POST", "/j")
        rt = await _drive(tb, "POST", "/j")
        # Both must agree on the status code.
        assert ru.status_code == rt.status_code, (
            f"missing-body status divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Custom exception handler
# ────────────────────────────────────────────────────────────────────

class _MyError(Exception):
    pass


def _custom_handler_app(fa_mod, resp_mod):
    JSONResponse = resp_mod.JSONResponse
    app = fa_mod.FastAPI()

    @app.exception_handler(_MyError)
    async def _h(_request, _exc):
        return JSONResponse({"caught": True}, status_code=499)

    @app.get("/raise")
    def _r():
        raise _MyError()

    return app


def test_custom_exception_handler_parity():
    up, tb = _build(_custom_handler_app)

    async def go():
        ru = await _drive(up, "GET", "/raise")
        rt = await _drive(tb, "GET", "/raise")
        assert ru.status_code == 499
        assert rt.status_code == 499, (
            f"turbo ignored custom exception_handler: status={rt.status_code} "
            f"body={rt.content!r}"
        )
        assert ru.json() == {"caught": True}
        assert rt.json() == {"caught": True}

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Response Content-Type defaults
# ────────────────────────────────────────────────────────────────────

def _default_dict_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/d")
    def _d():
        return {"a": 1}

    return app


def test_default_json_response_content_type():
    """Returning a dict from a FastAPI handler yields a JSONResponse
    with Content-Type ``application/json`` (NO charset suffix —
    Starlette's JSONResponse leaves the media_type bare). Turbo
    must match."""
    up, tb = _build(_default_dict_app)

    async def go():
        ru = await _drive(up, "GET", "/d")
        rt = await _drive(tb, "GET", "/d")
        assert ru.headers["content-type"] == rt.headers["content-type"], (
            f"JSON Content-Type divergence: upstream={ru.headers['content-type']!r} "
            f"turbo={rt.headers['content-type']!r}"
        )
        # Both should be plain application/json — no charset.
        assert ru.headers["content-type"] == "application/json", (
            ru.headers["content-type"]
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Bytes-yielding StreamingResponse: media_type should pass through
# ────────────────────────────────────────────────────────────────────

def _binary_stream_app(fa_mod, resp_mod):
    Streaming = resp_mod.StreamingResponse
    app = fa_mod.FastAPI()

    def _bin():
        yield b"\x00\x01\x02"
        yield b"\x03\x04\x05"

    @app.get("/b")
    def _b():
        return Streaming(_bin(), media_type="application/octet-stream")

    return app


def test_binary_streaming_content_type_parity():
    up, tb = _build(_binary_stream_app)

    async def go():
        ru = await _drive(up, "GET", "/b")
        rt = await _drive(tb, "GET", "/b")
        assert ru.status_code == 200
        assert rt.status_code == 200
        assert ru.content == b"\x00\x01\x02\x03\x04\x05"
        assert rt.content == ru.content
        assert ru.headers["content-type"] == "application/octet-stream"
        assert rt.headers["content-type"] == "application/octet-stream", (
            rt.headers["content-type"]
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Request inspection: body / json / headers
# ────────────────────────────────────────────────────────────────────

def _request_intro_app(fa_mod, resp_mod):
    JSONResponse = resp_mod.JSONResponse
    app = fa_mod.FastAPI()

    @app.post("/echo-headers")
    async def _eh(request: fa_mod.Request):
        # Echo selected request features so we can compare across
        # stacks: header lookup, query, path, method.
        return JSONResponse({
            "method": request.method,
            "path": request.url.path,
            "ua": request.headers.get("user-agent"),
            "ct": request.headers.get("content-type"),
            "x_custom": request.headers.get("x-custom"),
        })

    return app


def test_request_introspection_parity():
    up, tb = _build(_request_intro_app)

    async def go():
        headers = {
            "user-agent": "ParityTest/1.0",
            "x-custom": "hello",
            "content-type": "application/json",
        }
        ru = await _drive(up, "POST", "/echo-headers", json={}, headers=headers)
        rt = await _drive(tb, "POST", "/echo-headers", json={}, headers=headers)
        assert ru.json() == rt.json(), (
            f"request introspection divergence: upstream={ru.json()!r} "
            f"turbo={rt.json()!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# urlencoded form body
# ────────────────────────────────────────────────────────────────────

def _form_app(fa_mod, _resp_mod):
    Form = fa_mod.Form
    app = fa_mod.FastAPI()

    @app.post("/form")
    def _f(name: str = Form(...), age: int = Form(...)):
        return {"name": name, "age": age}

    return app


def test_urlencoded_form_parity():
    up, tb = _build(_form_app)

    async def go():
        ru = await _drive(up, "POST", "/form", data={"name": "x", "age": "30"})
        rt = await _drive(tb, "POST", "/form", data={"name": "x", "age": "30"})
        assert ru.status_code == 200, (ru.status_code, ru.content)
        assert rt.status_code == 200, (rt.status_code, rt.content)
        assert ru.json() == rt.json() == {"name": "x", "age": 30}

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Empty list / None / boolean as response value
# ────────────────────────────────────────────────────────────────────

def _scalar_returns_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/empty-list")
    def _el():
        return []

    @app.get("/none")
    def _n():
        return None

    @app.get("/bool")
    def _b():
        return True

    @app.get("/int")
    def _i():
        return 42

    @app.get("/string")
    def _s():
        return "hello"

    return app


def test_scalar_response_parity():
    """Returning scalars / empty containers from a handler must
    serialize identically across the two stacks."""
    up, tb = _build(_scalar_returns_app)

    async def go():
        for path in ("/empty-list", "/none", "/bool", "/int", "/string"):
            ru = await _drive(up, "GET", path)
            rt = await _drive(tb, "GET", path)
            assert ru.status_code == rt.status_code, (
                f"{path}: status divergence {ru.status_code} vs {rt.status_code}"
            )
            assert ru.content == rt.content, (
                f"{path}: body divergence upstream={ru.content!r} "
                f"turbo={rt.content!r}"
            )
            assert ru.headers.get("content-type") == rt.headers.get("content-type"), (
                f"{path}: content-type divergence "
                f"upstream={ru.headers.get('content-type')!r} "
                f"turbo={rt.headers.get('content-type')!r}"
            )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Path with no trailing slash on a "/" route
# ────────────────────────────────────────────────────────────────────

def _root_route_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/")
    def _r():
        return {"ok": True}

    return app


def test_root_route_no_trailing_slash_redirect():
    """Hitting ``/`` directly should NOT trigger redirect_slashes
    (already canonical). Both stacks must serve 200."""
    up, tb = _build(_root_route_app)

    async def go():
        ru = await _drive(up, "GET", "/")
        rt = await _drive(tb, "GET", "/")
        assert ru.status_code == 200
        assert rt.status_code == 200

    _run(go())


# ────────────────────────────────────────────────────────────────────
# 405 must include Allow header listing supported methods
# ────────────────────────────────────────────────────────────────────

def _multi_method_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/r")
    def _g():
        return {"m": "get"}

    @app.post("/r")
    def _p():
        return {"m": "post"}

    return app


def test_405_allow_header_parity():
    up, tb = _build(_multi_method_app)

    async def go():
        ru = await _drive(up, "PUT", "/r")
        rt = await _drive(tb, "PUT", "/r")
        assert ru.status_code == 405
        assert rt.status_code == 405
        # Whatever upstream advertises in Allow, turbo must match.
        # Starlette's matcher stops at the first route whose path
        # matches and reports that route's methods only — so for
        # ``@app.get("/r")`` then ``@app.post("/r")`` (two distinct
        # routes), Allow = "GET" because the GET route is matched
        # first. Turbo must not accumulate across all matching paths.
        up_allow = set(m.strip() for m in ru.headers.get("allow", "").split(","))
        tb_allow = set(m.strip() for m in rt.headers.get("allow", "").split(","))
        assert tb_allow == up_allow, (
            f"Allow header divergence: upstream={up_allow} turbo={tb_allow}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Cookie attributes
# ────────────────────────────────────────────────────────────────────

def _cookie_attrs_app(fa_mod, resp_mod):
    Resp = resp_mod.Response
    app = fa_mod.FastAPI()

    @app.get("/c")
    def _c():
        r = Resp(content=b"ok")
        r.set_cookie(
            "auth",
            "token",
            max_age=3600,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/api",
        )
        return r

    return app


def test_cookie_attributes_parity():
    """``set_cookie(httponly=True, secure=True, samesite=…)`` must
    produce a Set-Cookie header with the same attribute set across
    both stacks."""
    up, tb = _build(_cookie_attrs_app)

    async def go():
        ru = await _drive(up, "GET", "/c")
        rt = await _drive(tb, "GET", "/c")
        # Compare the raw Set-Cookie bytes (lowercased for stability).
        # Both stacks should emit the same attributes — order may
        # differ but the set of attributes shouldn't.
        up_sc = ru.headers.get("set-cookie", "").lower()
        tb_sc = rt.headers.get("set-cookie", "").lower()
        for attr in ("httponly", "secure", "samesite=strict", "max-age=3600", "path=/api"):
            assert attr in up_sc, f"upstream missing {attr}: {up_sc!r}"
            assert attr in tb_sc, f"turbo missing {attr}: {tb_sc!r}"

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Datetime / UUID JSON serialisation in response_model
# ────────────────────────────────────────────────────────────────────

def _datetime_app(fa_mod, _resp_mod):
    from datetime import datetime, timezone
    from pydantic import BaseModel

    class M(BaseModel):
        ts: datetime

    app = fa_mod.FastAPI()

    @app.get("/dt", response_model=M)
    def _dt():
        return M(ts=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    return app


def test_datetime_serialization_parity():
    up, tb = _build(_datetime_app)

    async def go():
        ru = await _drive(up, "GET", "/dt")
        rt = await _drive(tb, "GET", "/dt")
        assert ru.status_code == 200
        assert rt.status_code == 200
        body_u = ru.json()
        body_t = rt.json()
        assert body_u == body_t, (
            f"datetime serialization divergence: upstream={body_u!r} "
            f"turbo={body_t!r}"
        )

    _run(go())


def _uuid_app(fa_mod, _resp_mod):
    from uuid import UUID
    from pydantic import BaseModel

    class M(BaseModel):
        u: UUID

    app = fa_mod.FastAPI()

    @app.get("/u", response_model=M)
    def _u():
        return M(u=UUID("12345678-1234-5678-1234-567812345678"))

    return app


def test_uuid_serialization_parity():
    up, tb = _build(_uuid_app)

    async def go():
        ru = await _drive(up, "GET", "/u")
        rt = await _drive(tb, "GET", "/u")
        assert ru.json() == rt.json(), (
            f"UUID serialization divergence: upstream={ru.json()!r} "
            f"turbo={rt.json()!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# APIRouter with prefix
# ────────────────────────────────────────────────────────────────────

def _router_prefix_app(fa_mod, _resp_mod):
    Router = fa_mod.APIRouter
    app = fa_mod.FastAPI()
    r = Router(prefix="/api/v1")

    @r.get("/items/{x}")
    def _i(x: int):
        return {"x": x}

    app.include_router(r)
    return app


def test_router_prefix_parity():
    up, tb = _build(_router_prefix_app)

    async def go():
        ru = await _drive(up, "GET", "/api/v1/items/42")
        rt = await _drive(tb, "GET", "/api/v1/items/42")
        assert ru.status_code == 200
        assert rt.status_code == 200
        assert ru.json() == {"x": 42}
        assert rt.json() == {"x": 42}

        # Wrong prefix → 404 in both.
        ru = await _drive(up, "GET", "/api/v1/nope")
        rt = await _drive(tb, "GET", "/api/v1/nope")
        assert ru.status_code == 404
        assert rt.status_code == 404

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Path-type parameter (matches slashes)
# ────────────────────────────────────────────────────────────────────

def _path_type_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/files/{p:path}")
    def _f(p: str):
        return {"p": p}

    return app


def test_path_type_param_parity():
    """``{p:path}`` matches across slashes in upstream — turbo must
    match the same."""
    up, tb = _build(_path_type_app)

    async def go():
        ru = await _drive(up, "GET", "/files/a/b/c.txt")
        rt = await _drive(tb, "GET", "/files/a/b/c.txt")
        assert ru.status_code == 200
        assert rt.status_code == 200
        assert ru.json() == {"p": "a/b/c.txt"}
        assert rt.json() == {"p": "a/b/c.txt"}

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Custom default Response content-type override on FastAPI()
# ────────────────────────────────────────────────────────────────────

def _custom_default_app(fa_mod, resp_mod):
    HTMLResp = resp_mod.HTMLResponse
    app = fa_mod.FastAPI(default_response_class=HTMLResp)

    @app.get("/h")
    def _h():
        return "<p>hi</p>"

    return app


def test_default_response_class_parity():
    """``FastAPI(default_response_class=HTMLResponse)`` — handlers
    that return strings should serialize as HTML instead of JSON."""
    up, tb = _build(_custom_default_app)

    async def go():
        ru = await _drive(up, "GET", "/h")
        rt = await _drive(tb, "GET", "/h")
        # Both stacks must agree on Content-Type.
        assert ru.headers["content-type"] == rt.headers["content-type"], (
            f"Content-Type divergence: upstream={ru.headers['content-type']!r} "
            f"turbo={rt.headers['content-type']!r}"
        )
        # And the body should NOT be JSON-quoted.
        assert ru.content == rt.content, (
            f"body divergence: upstream={ru.content!r} turbo={rt.content!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Query param list aggregation
# ────────────────────────────────────────────────────────────────────

def _query_list_app(fa_mod, _resp_mod):
    Query = fa_mod.Query
    app = fa_mod.FastAPI()

    @app.get("/items")
    def _i(tag: list[str] = Query(default=[])):
        return {"tags": tag}

    return app


def test_query_list_aggregation_parity():
    """``?tag=a&tag=b&tag=c`` with ``Query()`` marker should produce
    ``["a","b","c"]`` in both stacks. (Without a Query marker,
    ``list[str]`` is classified as a body param and the query is
    ignored — that case is covered by
    ``test_bare_list_param_default_when_no_body``.)"""
    up, tb = _build(_query_list_app)

    async def go():
        ru = await _drive(up, "GET", "/items?tag=a&tag=b&tag=c")
        rt = await _drive(tb, "GET", "/items?tag=a&tag=b&tag=c")
        assert ru.status_code == 200, (ru.status_code, ru.content)
        assert rt.status_code == 200, (rt.status_code, rt.content)
        assert ru.json() == {"tags": ["a", "b", "c"]}
        assert rt.json() == {"tags": ["a", "b", "c"]}

    _run(go())


def _bare_list_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/items")
    def _i(tag: list[str] = []):
        return {"tags": tag}

    return app


def test_bare_list_param_default_when_no_body():
    """Bare ``tag: list[str] = []`` (no ``Query()``) is body-typed
    in upstream FastAPI. With no request body, the default ``[]``
    should be used — NOT a 422 missing-body error."""
    up, tb = _build(_bare_list_app)

    async def go():
        ru = await _drive(up, "GET", "/items")
        rt = await _drive(tb, "GET", "/items")
        assert ru.status_code == 200, (ru.status_code, ru.content)
        assert rt.status_code == 200, (
            f"turbo rejects body-typed param with default: "
            f"status={rt.status_code} body={rt.content!r}"
        )
        assert ru.json() == {"tags": []}
        assert rt.json() == {"tags": []}

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Header injection from a handler-returned Response
# ────────────────────────────────────────────────────────────────────

def _custom_header_app(fa_mod, resp_mod):
    JSONResp = resp_mod.JSONResponse
    app = fa_mod.FastAPI()

    @app.get("/h")
    def _h():
        return JSONResp({"ok": True}, headers={"X-Trace-Id": "abc-123"})

    return app


def test_response_custom_header_parity():
    up, tb = _build(_custom_header_app)

    async def go():
        ru = await _drive(up, "GET", "/h")
        rt = await _drive(tb, "GET", "/h")
        assert ru.headers.get("x-trace-id") == "abc-123"
        assert rt.headers.get("x-trace-id") == "abc-123", (
            f"turbo dropped custom header: {dict(rt.headers)}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# response_class mismatch: handler returns dict but response_class
# expects str — must surface as an error, NOT silently fall back to
# JSON 200.
# ────────────────────────────────────────────────────────────────────

def _bad_response_class_app(fa_mod, resp_mod):
    """Handler returns a dict, but ``response_class=PlainTextResponse``
    expects a string. Upstream FastAPI raises (the response_class
    constructor blows up), which surfaces as a 500. Turbo must NOT
    silently fall back to a 200 JSON envelope — that hides real
    application bugs."""
    PlainText = resp_mod.PlainTextResponse
    app = fa_mod.FastAPI()

    @app.get("/bad", response_class=PlainText)
    def _b():
        return {"a": 1}

    return app


def test_response_class_construction_error_surfaces():
    up, tb = _build(_bad_response_class_app)

    async def go():
        ru = await _drive(up, "GET", "/bad")
        rt = await _drive(tb, "GET", "/bad")
        # Whatever upstream does (typically 500 or non-200), turbo
        # must match — silently returning 200 would mask the bug.
        assert rt.status_code == ru.status_code, (
            f"response_class construction-error parity: "
            f"upstream={ru.status_code} turbo={rt.status_code}"
        )
        # Specifically: must NOT be a successful 200.
        assert rt.status_code != 200, (
            f"turbo silently swallowed response_class construction error: "
            f"status=200 body={rt.content!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Router-level default_response_class cascade
# ────────────────────────────────────────────────────────────────────

def _router_default_response_class_app(fa_mod, resp_mod):
    HTMLResp = resp_mod.HTMLResponse
    Router = fa_mod.APIRouter
    app = fa_mod.FastAPI()
    r = Router(default_response_class=HTMLResp)

    @r.get("/h")
    def _h():
        return "<p>hi</p>"

    app.include_router(r)
    return app


def test_router_default_response_class_cascade():
    """``APIRouter(default_response_class=HTMLResponse)`` — handlers
    on that router that return strings must serialize as HTML, not
    JSON. The cascade is route → router → app default → JSONResponse."""
    up, tb = _build(_router_default_response_class_app)

    async def go():
        ru = await _drive(up, "GET", "/h")
        rt = await _drive(tb, "GET", "/h")
        assert ru.headers["content-type"] == rt.headers["content-type"], (
            f"router default_response_class cascade broken: "
            f"upstream={ru.headers['content-type']!r} "
            f"turbo={rt.headers['content-type']!r}"
        )
        assert ru.content == rt.content

    _run(go())


# ────────────────────────────────────────────────────────────────────
# include_router(..., default_response_class=...) cascade
# ────────────────────────────────────────────────────────────────────

def _include_default_response_class_app(fa_mod, resp_mod):
    HTMLResp = resp_mod.HTMLResponse
    Router = fa_mod.APIRouter
    app = fa_mod.FastAPI()
    r = Router()

    @r.get("/h2")
    def _h():
        return "<p>hi2</p>"

    app.include_router(r, default_response_class=HTMLResp)
    return app


def test_include_router_default_response_class_cascade():
    """``include_router(..., default_response_class=HTMLResponse)``
    must apply to all included routes."""
    up, tb = _build(_include_default_response_class_app)

    async def go():
        ru = await _drive(up, "GET", "/h2")
        rt = await _drive(tb, "GET", "/h2")
        assert ru.headers["content-type"] == rt.headers["content-type"], (
            f"include-level default_response_class cascade broken: "
            f"upstream={ru.headers['content-type']!r} "
            f"turbo={rt.headers['content-type']!r}"
        )
        assert ru.content == rt.content

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Trailing-slash redirect: absolute Location URL
# ────────────────────────────────────────────────────────────────────

def test_trailing_slash_redirect_location_is_absolute():
    """Upstream's redirect_slashes middleware emits an ABSOLUTE
    Location URL (``http://t/items``), not a path-only one
    (``/items``). Some HTTP clients balk at relative redirects in
    response to a Location header."""
    up, tb = _build(_trailing_slash_app)

    async def go():
        ru = await _drive(up, "GET", "/items/", follow_redirects=False)
        rt = await _drive(tb, "GET", "/items/", follow_redirects=False)
        assert ru.status_code == 307
        assert rt.status_code == 307
        # Both must have Location pointing at the canonical resource.
        # Compare the Location headers directly — turbo must match
        # upstream's exact format (absolute or relative, whichever
        # upstream chose).
        assert rt.headers["location"] == ru.headers["location"], (
            f"redirect Location divergence: upstream={ru.headers['location']!r} "
            f"turbo={rt.headers['location']!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Custom APIRoute(route_class=...) wrapper
# ────────────────────────────────────────────────────────────────────

def _custom_route_class_app(fa_mod, resp_mod):
    """Custom APIRoute subclass overrides ``get_route_handler`` to
    add an ``X-Wrapped`` header. Both stacks must invoke the
    override — turbo's in-process path was previously unwrapping
    to the bare endpoint and skipping the wrapper."""
    Routing = fa_mod.routing
    APIRoute = Routing.APIRoute
    JSONResponse = resp_mod.JSONResponse

    class WrappedRoute(APIRoute):
        def get_route_handler(self):
            inner = super().get_route_handler()

            async def _wrapped(request):
                response = await inner(request)
                response.headers["X-Wrapped"] = "yes"
                return response

            return _wrapped

    Router = fa_mod.APIRouter
    app = fa_mod.FastAPI()
    r = Router(route_class=WrappedRoute)

    @r.get("/w")
    def _w():
        return JSONResponse({"ok": True})

    app.include_router(r)
    return app


def test_custom_route_class_wrapper_runs():
    """Custom APIRoute subclass must wrap the handler; the
    ``X-Wrapped`` header must appear in the response."""
    up, tb = _build(_custom_route_class_app)

    async def go():
        ru = await _drive(up, "GET", "/w")
        rt = await _drive(tb, "GET", "/w")
        assert ru.headers.get("x-wrapped") == "yes", (
            f"upstream missing X-Wrapped header: {dict(ru.headers)}"
        )
        assert rt.headers.get("x-wrapped") == "yes", (
            f"turbo bypassed custom APIRoute.get_route_handler: "
            f"headers={dict(rt.headers)}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# CORS / GZip / HTTPSRedirect middleware applied in-process
# ────────────────────────────────────────────────────────────────────

def _cors_app(fa_mod, _resp_mod):
    from starlette.middleware.cors import CORSMiddleware
    app = fa_mod.FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://example.com"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/c")
    def _c():
        return {"ok": True}

    return app


def test_cors_preflight_in_process():
    """OPTIONS preflight with ``Origin`` + ``Access-Control-Request-
    Method`` should be handled by CORSMiddleware — even on the in-
    process ASGI path. Turbo previously only registered CORS as a
    Tower layer for the Rust server."""
    up, tb = _build(_cors_app)

    async def go():
        headers = {
            "origin": "http://example.com",
            "access-control-request-method": "GET",
        }
        ru = await _drive(up, "OPTIONS", "/c", headers=headers)
        rt = await _drive(tb, "OPTIONS", "/c", headers=headers)
        assert ru.status_code == rt.status_code, (
            f"CORS preflight status divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )
        # Both should advertise allow-origin.
        assert ru.headers.get("access-control-allow-origin") == "http://example.com"
        assert (
            rt.headers.get("access-control-allow-origin") == "http://example.com"
        ), f"turbo missing CORS preflight response: {dict(rt.headers)}"

    _run(go())


def _gzip_app(fa_mod, _resp_mod):
    from starlette.middleware.gzip import GZipMiddleware
    app = fa_mod.FastAPI()
    # minimum_size=10 so even small responses get compressed.
    app.add_middleware(GZipMiddleware, minimum_size=10)

    @app.get("/g")
    def _g():
        # Make the body large enough that gzip is profitable.
        return {"data": "x" * 1024}

    return app


def test_gzip_in_process_compresses_response():
    up, tb = _build(_gzip_app)

    async def go():
        ru = await _drive(up, "GET", "/g", headers={"accept-encoding": "gzip"})
        rt = await _drive(tb, "GET", "/g", headers={"accept-encoding": "gzip"})
        assert ru.headers.get("content-encoding") == "gzip", (
            f"upstream didn't compress: {dict(ru.headers)}"
        )
        assert rt.headers.get("content-encoding") == "gzip", (
            f"turbo GZipMiddleware not applied in-process: "
            f"headers={dict(rt.headers)}"
        )

    _run(go())


def _https_redirect_app(fa_mod, _resp_mod):
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
    app = fa_mod.FastAPI()
    app.add_middleware(HTTPSRedirectMiddleware)

    @app.get("/r")
    def _r():
        return {"ok": True}

    return app


def test_https_redirect_in_process():
    up, tb = _build(_https_redirect_app)

    async def go():
        ru = await _drive(up, "GET", "/r", follow_redirects=False)
        rt = await _drive(tb, "GET", "/r", follow_redirects=False)
        # HTTPSRedirectMiddleware sends a 307 redirect from http to https.
        assert ru.status_code == rt.status_code, (
            f"HTTPSRedirect status divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )
        assert ru.status_code in (301, 307, 308)

    _run(go())


# ────────────────────────────────────────────────────────────────────
# BaseHTTPMiddleware: request.state propagation + ordering
# ────────────────────────────────────────────────────────────────────

def _basehttp_state_app(fa_mod, _resp_mod):
    from starlette.middleware.base import BaseHTTPMiddleware

    class _Marker(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.seen = True
            response = await call_next(request)
            response.headers["x-mw-saw"] = "yes"
            return response

    app = fa_mod.FastAPI()
    app.add_middleware(_Marker)

    @app.get("/state")
    def _h(request: fa_mod.Request):
        return {"seen": getattr(request.state, "seen", False)}

    return app


def test_basehttp_middleware_state_propagates_to_endpoint():
    """BaseHTTPMiddleware mutations to ``request.state`` MUST be
    visible to the endpoint that injects ``request: Request``.
    Starlette achieves this by storing state on the scope; turbo
    used to give each Request its own ``_state`` instance, dropping
    every middleware mutation."""
    up, tb = _build(_basehttp_state_app)

    async def go():
        ru = await _drive(up, "GET", "/state")
        rt = await _drive(tb, "GET", "/state")
        assert ru.json() == {"seen": True}
        assert rt.json() == {"seen": True}, (
            f"turbo lost middleware state mutation: {rt.json()!r}"
        )
        assert ru.headers.get("x-mw-saw") == "yes"
        assert rt.headers.get("x-mw-saw") == "yes"

    _run(go())


def _http_middleware_order_app(fa_mod, _resp_mod):
    """Three middlewares appended in order. With FA semantics
    (last-registered outermost), the response should carry the
    headers in the order the wrappers fire — outermost wrapper
    appends LAST so its header value wins ``x-order`` inspection.
    """
    app = fa_mod.FastAPI()

    @app.middleware("http")
    async def _mw1(request, call_next):
        response = await call_next(request)
        # Append our identifier to whatever the inner MW set.
        existing = response.headers.get("x-order", "")
        response.headers["x-order"] = (existing + ",mw1").lstrip(",")
        return response

    @app.middleware("http")
    async def _mw2(request, call_next):
        response = await call_next(request)
        existing = response.headers.get("x-order", "")
        response.headers["x-order"] = (existing + ",mw2").lstrip(",")
        return response

    @app.middleware("http")
    async def _mw3(request, call_next):
        response = await call_next(request)
        existing = response.headers.get("x-order", "")
        response.headers["x-order"] = (existing + ",mw3").lstrip(",")
        return response

    @app.get("/")
    def _h():
        return {"ok": True}

    return app


def test_http_middleware_registration_order_parity():
    """Three ``@app.middleware('http')`` decorators in declaration
    order MW1, MW2, MW3. Each appends its name to ``x-order`` after
    receiving the inner response. FA semantics: MW3 (last-registered)
    is outermost, so it appends LAST. Final value: ``mw1,mw2,mw3``.

    Turbo previously iterated ``reversed(http_mws)`` for chain
    construction, making MW1 outermost — final value would be
    ``mw3,mw2,mw1``."""
    up, tb = _build(_http_middleware_order_app)

    async def go():
        ru = await _drive(up, "GET", "/")
        rt = await _drive(tb, "GET", "/")
        assert ru.headers["x-order"] == rt.headers["x-order"], (
            f"middleware order divergence: upstream={ru.headers['x-order']!r} "
            f"turbo={rt.headers['x-order']!r}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Custom APIRoute(route_class=...) must NOT bypass app HTTP middleware
# ────────────────────────────────────────────────────────────────────

def _custom_route_with_app_mw_app(fa_mod, resp_mod):
    Routing = fa_mod.routing
    APIRoute = Routing.APIRoute
    JSONResponse = resp_mod.JSONResponse

    class WrappedRoute(APIRoute):
        def get_route_handler(self):
            inner = super().get_route_handler()

            async def _wrapped(request):
                response = await inner(request)
                response.headers["X-Route"] = "yes"
                return response

            return _wrapped

    Router = fa_mod.APIRouter
    app = fa_mod.FastAPI()

    @app.middleware("http")
    async def _app_mw(request, call_next):
        response = await call_next(request)
        response.headers["X-App-Mw"] = "yes"
        return response

    r = Router(route_class=WrappedRoute)

    @r.get("/w")
    def _w():
        return JSONResponse({"ok": True})

    app.include_router(r)
    return app


def test_custom_route_class_runs_app_http_middleware():
    """A route registered through a custom ``route_class`` that wraps
    ``get_route_handler`` must STILL run the app-level HTTP middleware
    chain. Both ``X-Route`` (from the route wrapper) and ``X-App-Mw``
    (from the app middleware) must appear on the response."""
    up, tb = _build(_custom_route_with_app_mw_app)

    async def go():
        ru = await _drive(up, "GET", "/w")
        rt = await _drive(tb, "GET", "/w")
        assert ru.headers.get("x-route") == "yes"
        assert ru.headers.get("x-app-mw") == "yes"
        assert rt.headers.get("x-route") == "yes", (
            f"turbo dropped route-class wrapper: {dict(rt.headers)}"
        )
        assert rt.headers.get("x-app-mw") == "yes", (
            f"turbo bypassed app HTTP middleware on custom-route path: "
            f"{dict(rt.headers)}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Nested router default_response_class inheritance
# ────────────────────────────────────────────────────────────────────

def _nested_router_default_app(fa_mod, resp_mod):
    HTMLResp = resp_mod.HTMLResponse
    Router = fa_mod.APIRouter

    inner = Router()

    @inner.get("/h")
    def _h():
        return "<p>nested</p>"

    parent = Router(default_response_class=HTMLResp)
    parent.include_router(inner)

    app = fa_mod.FastAPI()
    app.include_router(parent)
    return app


def test_nested_router_default_response_class_inheritance():
    """``APIRouter(default_response_class=HTMLResponse)`` must
    propagate to routes inside a CHILD router that doesn't set its
    own default. Upstream Starlette walks the inheritance chain;
    turbo's stamp logic only looked at the current include's kwarg
    or the immediate src_router's default, missing the parent."""
    up, tb = _build(_nested_router_default_app)

    async def go():
        ru = await _drive(up, "GET", "/h")
        rt = await _drive(tb, "GET", "/h")
        assert ru.headers["content-type"] == rt.headers["content-type"], (
            f"nested default_response_class divergence: "
            f"upstream={ru.headers['content-type']!r} "
            f"turbo={rt.headers['content-type']!r}"
        )
        assert ru.content == rt.content

    _run(go())


# ────────────────────────────────────────────────────────────────────
# Mixed Tower-bound + raw ASGI middleware ordering
# ────────────────────────────────────────────────────────────────────

def _mixed_mw_order_app(fa_mod, _resp_mod):
    """User registers HTTPSRedirectMiddleware FIRST, then a custom
    ASGI middleware that decorates every response with ``X-After``.
    With FA's last-registered-outermost semantics, the custom MW
    wraps HTTPSRedirect and gets to decorate the redirect response.
    """
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    class DecoratorMW:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            async def _send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"x-after", b"yes"))
                    message = dict(message)
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, _send_wrapper)

    app = fa_mod.FastAPI()
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(DecoratorMW)

    @app.get("/r")
    def _r():
        return {"ok": True}

    return app


def test_mixed_tower_and_raw_asgi_middleware_order():
    """The custom ASGI MW added AFTER ``HTTPSRedirectMiddleware``
    should be OUTERMOST and decorate the 307 redirect response."""
    up, tb = _build(_mixed_mw_order_app)

    async def go():
        ru = await _drive(up, "GET", "/r", follow_redirects=False)
        rt = await _drive(tb, "GET", "/r", follow_redirects=False)
        # Both redirect.
        assert ru.status_code in (301, 307, 308)
        assert rt.status_code == ru.status_code
        # Both should carry x-after on the redirect response (the
        # outer MW decorated it).
        assert ru.headers.get("x-after") == "yes", (
            f"upstream missing x-after: {dict(ru.headers)}"
        )
        assert rt.headers.get("x-after") == "yes", (
            f"turbo failed to apply MW added AFTER Tower-bound MW: "
            f"{dict(rt.headers)}"
        )

    _run(go())
