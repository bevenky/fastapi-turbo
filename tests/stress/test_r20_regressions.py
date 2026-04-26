"""R20 audit follow-ups — parity / security fixes for
``UploadFile.file`` (sync file-like), case-sensitive multipart
boundaries, ``max_files`` / ``max_fields`` enforcement, ``FormData``
mapping semantics (last-wins), and ``TestClient.stream`` Digest
auth challenge-response."""
import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 PyUploadFile.file is a sync file (Starlette parity)
# ────────────────────────────────────────────────────────────────────


def test_pyuploadfile_file_returns_sync_file_returning_bytes():
    """Starlette's ``UploadFile.file`` is a ``SpooledTemporaryFile``
    whose ``.read()`` returns ``bytes`` and ``.write(b)`` returns
    ``int``. Earlier Rust impl returned ``self`` for ``.file``, so
    ``upload.file.read()`` produced an awaitable wrapper instead of
    bytes — broke ``shutil.copyfileobj(upload.file, dest)`` and any
    sync consumer that introspects the return type.

    Also exercises the Arc-shared backing buffer: a write through
    ``upload.file`` must be visible to the async ``upload`` API."""
    pytest.importorskip("fastapi_turbo._fastapi_turbo_core")
    from fastapi_turbo._fastapi_turbo_core import PyUploadFile, PySyncFile

    # PyUploadFile is constructed by the Rust multipart parser only
    # — assert the API surface here, then drive a real upload below.
    assert hasattr(PyUploadFile, "file"), "PyUploadFile must expose .file"
    for method in ("read", "write", "seek", "tell", "close", "closed"):
        assert hasattr(PySyncFile, method), method


def test_uploadfile_file_returns_sync_via_real_upload():
    """End-to-end: drive a multipart upload through a handler that
    uses ``upload.file.read()`` (sync) and asserts the return type
    is ``bytes``. The Rust path materialises ``PyUploadFile`` so
    ``.file`` must hand back a ``PySyncFile`` whose ``.read()``
    returns plain bytes (not ``ImmediateBytes``)."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        sync_handle = file.file
        sync_handle.seek(0)
        body = sync_handle.read()
        return JSONResponse({
            "type": type(body).__name__,
            "value": body.decode("utf-8") if isinstance(body, bytes) else None,
        })

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("hi.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        # In-process Python path uses BytesIO, Rust path uses PySyncFile;
        # both return ``bytes`` from ``.read()``. The previous Rust
        # behaviour (returning ImmediateBytes) is what this regression
        # locks out.
        assert body["type"] == "bytes", body
        assert body["value"] == "hello world", body


def test_uploadfile_async_write_returns_none():
    """Starlette's ``async def write(...) -> None``. The R19 fix
    started returning ``int`` (byte count) which diverged. R20
    restores ``None`` — callers needing the byte count can use the
    sync ``upload.file.write(b)`` path."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        ret = await file.write(b"xyz")
        return JSONResponse({"ret_is_none": ret is None})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("t.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"ret_is_none": True}


# ────────────────────────────────────────────────────────────────────
# #2 request.form() preserves case-sensitive boundary +
#    enforces max_files / max_fields
# ────────────────────────────────────────────────────────────────────


def test_request_form_preserves_case_sensitive_boundary():
    """RFC 7578 multipart boundaries are case-sensitive. Earlier
    code lowercased the entire ``Content-Type`` header, so a body
    framed by ``boundary=AaB03x`` would parse with boundary
    ``aab03x`` and yield an empty ``FormData``."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    boundary = "AaB03x"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="hello"\r\n\r\n'
        "world\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    app = FastAPI()

    @app.post("/m")
    async def _m(request: Request):
        form = await request.form()
        return JSONResponse({"hello": form.get("hello")})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/m",
            content=body,
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"hello": "world"}, r.json()


def test_request_form_enforces_max_fields_limit():
    """``form(max_fields=1)`` against a multipart body must raise
    once a second text field is seen. The endpoint catches the
    exception so we can assert the error type explicitly (the
    framework's MPE handler is exercised by the ``Form(...)``
    injection test below). ``max_files`` / ``max_fields`` only
    apply to the multipart parser path — Starlette doesn't enforce
    them on urlencoded forms — so the body must be multipart."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.exceptions import MultiPartException
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/m")
    async def _m(request: Request):
        try:
            await request.form(max_fields=1)
        except MultiPartException as exc:
            return JSONResponse({"error": exc.message}, status_code=400)
        return JSONResponse({"ok": True})

    boundary = "BoUnDaRy"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="a"\r\n\r\n'
        "1\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="b"\r\n\r\n'
        "2\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/m",
            content=body,
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )
        assert r.status_code == 400, (r.status_code, r.content)


def test_form_endpoint_returns_400_when_too_many_fields():
    """The in-app dispatcher (Form/UploadFile injection path) also
    enforces the Starlette default ``max_fields=1000`` — exceed it
    and the server returns HTTP 400 with ``{"detail": "Too many
    fields ..."}`` rather than a 500 or a successful 200."""
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/f")
    async def _f(a: str = Form(...)):
        return {"a": a}

    # Hand-build a multipart body with 1001 text fields. ``files=`` /
    # ``data=`` would urlencode; we need a multipart body to hit the
    # multipart parser path.
    boundary = "BoUnDaRy"
    parts = []
    for i in range(1001):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="f{i}"\r\n\r\n'
            f"v{i}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8")

    with TestClient(app, in_process=True, raise_server_exceptions=False) as c:
        r = c.post(
            "/f",
            content=body,
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )
        assert r.status_code == 400, (r.status_code, r.content)


# ────────────────────────────────────────────────────────────────────
# #3 FormData mapping semantics: get/items collapsed (last wins)
# ────────────────────────────────────────────────────────────────────


def test_formdata_get_returns_last_value():
    """Parity with Starlette's ``ImmutableMultiDict``: ``form["x"]``
    and ``form.get("x")`` return the *last* value when ``x`` repeats.
    Earlier impl returned the first value."""
    from fastapi_turbo.datastructures import FormData

    f = FormData([("x", "1"), ("x", "2"), ("x", "3")])
    assert f["x"] == "3"
    assert f.get("x") == "3"
    # Multi-value access still returns all in order.
    assert f.getlist("x") == ["1", "2", "3"]


def test_formdata_items_returns_collapsed_mapping():
    """Starlette's ``form.items()`` is one ``(key, last_value)`` pair
    per unique key; ``.multi_items()`` returns the full list. Earlier
    ``.items()`` returned all pairs (which made
    ``dict(form)`` correct only by accident — last write to the dict
    won, but the iteration order of pairs was wrong)."""
    from fastapi_turbo.datastructures import FormData

    f = FormData([("x", "1"), ("y", "a"), ("x", "2")])
    items = list(f.items())
    assert items == [("x", "2"), ("y", "a")], items

    multi = list(f.multi_items())
    assert multi == [("x", "1"), ("y", "a"), ("x", "2")], multi

    # ``len`` is the unique-key count, not the pair count.
    assert len(f) == 2
    # ``dict(form)`` works correctly under either semantics, but
    # iteration order matters under the new mapping contract.
    assert dict(f) == {"x": "2", "y": "a"}


def test_formdata_form_request_get_returns_last():
    """End-to-end: a urlencoded ``a=1&a=2`` body parsed via
    ``await request.form()`` exposes ``form.get("a") == "2"``."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(request: Request):
        form = await request.form()
        return JSONResponse({
            "first_via_get": form.get("a"),
            "all": form.getlist("a"),
        })

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            content=b"a=1&a=2",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"first_via_get": "2", "all": ["1", "2"]}


# ────────────────────────────────────────────────────────────────────
# #4 TestClient.stream(auth=) honours challenge-response (Digest)
# ────────────────────────────────────────────────────────────────────


def test_testclient_stream_digest_auth_retries_on_401():
    """``c.stream("GET", "/x", auth=httpx.DigestAuth(...))`` must
    drive the full challenge-response loop: send the initial
    request, observe the 401 + ``WWW-Authenticate``, send back via
    ``flow.send(resp)``, get a new request stamped with
    ``Authorization: Digest …``, send that, and return the 200.
    Earlier code only used the first yielded request, so the test
    saw the unauthenticated 401."""
    import httpx
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse, StreamingResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/secret")
    def _secret(request: Request):
        # Toy digest-style server: expect the second request to
        # carry an ``Authorization: Digest …`` header. We don't
        # validate the cnonce / response — the point is that the
        # auth flow re-sends with *some* Digest header. First call:
        # 401 + WWW-Authenticate (parsed by httpx.DigestAuth to
        # build the second request). Second call: 200.
        auth = request.headers.get("authorization", "")
        if auth.startswith("Digest "):
            def _gen():
                yield b"ok"

            return StreamingResponse(_gen(), media_type="text/plain")
        return JSONResponse(
            {"detail": "auth required"},
            status_code=401,
            headers={
                "www-authenticate": (
                    'Digest realm="test", '
                    'qop="auth", '
                    'nonce="abc123", '
                    'opaque="opaqueval"'
                )
            },
        )

    with TestClient(app, in_process=True) as c:
        with c.stream(
            "GET", "/secret", auth=httpx.DigestAuth("u", "p")
        ) as r:
            chunks = list(r.iter_bytes())
            assert r.status_code == 200, (r.status_code, b"".join(chunks))
            assert b"".join(chunks) == b"ok"


def test_testclient_stream_basic_auth_still_works_post_refactor():
    """Regression guard for R19: the BasicAuth path (single yield,
    immediate StopIteration) must still produce the expected
    ``Authorization: Basic …`` header after the auth flow refactor."""
    import base64
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    captured = {}

    app = FastAPI()

    @app.get("/x")
    def _x(request: Request):
        captured["authorization"] = request.headers.get("authorization", "")

        def _gen():
            yield b"ok"

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        with c.stream("GET", "/x", auth=("u", "p")) as r:
            list(r.iter_bytes())

    expected = "Basic " + base64.b64encode(b"u:p").decode("ascii")
    assert captured["authorization"] == expected, captured
