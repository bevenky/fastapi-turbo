"""R23 audit follow-ups — ``Request.close`` propagates cleanup
errors, Rust ``PyUploadFile`` methods are real coroutine functions,
and the closed-file I/O guards are exercised over real-loopback
(not just the in-process ASGI path)."""
import asyncio
import inspect

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 Request.close() propagates cleanup errors
# ────────────────────────────────────────────────────────────────────


def test_request_close_propagates_uploadfile_close_errors():
    """Starlette's ``Request.close`` is a thin pass-through to
    ``self._form.close()`` — if an underlying ``UploadFile.close``
    raises, the caller sees it. Earlier impl wrapped in
    ``try/except Exception: pass``, which hid broken cleanup behind
    silent success and diverged from upstream (probe: upstream
    raised ``OSError("boom")``; we returned normally)."""
    from fastapi_turbo import UploadFile
    from fastapi_turbo.datastructures import FormData
    from fastapi_turbo.requests import Request

    class _BadUpload(UploadFile):
        def __init__(self):
            super().__init__(filename="x", file=None)

        async def close(self):
            raise OSError("boom")

    async def _run() -> str:
        req = Request(scope={"type": "http"})
        # Seed the parsed-form cache so Request.close has something
        # to close; FormData.close awaits each contained UploadFile.
        req._form = FormData([("file", _BadUpload())])
        try:
            await req.close()
        except OSError as exc:
            return str(exc)
        return ""

    assert asyncio.run(_run()) == "boom"


def test_request_close_when_no_form_parsed_is_silent_noop():
    """Regression guard: ``request.close`` over a request whose body
    was never parsed as a form must remain a clean no-op (the
    propagate-errors fix mustn't introduce noise on the common
    no-form path)."""
    from fastapi_turbo.requests import Request

    async def _run() -> bool:
        req = Request(scope={"type": "http"})
        await req.close()
        return True

    assert asyncio.run(_run()) is True


# ────────────────────────────────────────────────────────────────────
# #2 PyUploadFile methods are real coroutine functions
# ────────────────────────────────────────────────────────────────────


def test_pyuploadfile_read_is_coroutine_function():
    """Starlette's ``UploadFile.read`` is ``async def`` — libraries
    that introspect with ``inspect.iscoroutinefunction(file.read)``
    expect ``True``. The Rust-bound ``PyUploadFile.read`` returned a
    pre-built awaitable, so the FUNCTION wasn't a coroutine
    function. Now wrapped at module load with an ``async def`` shim
    that drives the original immediate awaitable."""
    pytest.importorskip("fastapi_turbo._fastapi_turbo_core")
    from fastapi_turbo._fastapi_turbo_core import PyUploadFile

    assert inspect.iscoroutinefunction(PyUploadFile.read)
    assert inspect.iscoroutinefunction(PyUploadFile.write)
    assert inspect.iscoroutinefunction(PyUploadFile.seek)
    assert inspect.iscoroutinefunction(PyUploadFile.close)


def test_pyuploadfile_read_still_returns_bytes_after_wrapper():
    """Wrapping the Rust methods with ``async def`` shims must not
    change the return values handlers see. ``await file.read()``
    still resolves to bytes, ``await file.close()`` to None, etc."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        body = await file.read()
        return JSONResponse({
            "type": type(body).__name__,
            "value": body.decode("utf-8") if isinstance(body, bytes) else None,
        })

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        assert body == {"type": "bytes", "value": "abc"}, body


# ────────────────────────────────────────────────────────────────────
# #3 closed-file I/O guards over the REAL Rust path
# ────────────────────────────────────────────────────────────────────
#
# The R22 tests used ``in_process=True`` which dispatches through
# the in-process ASGI adapter — ``UploadFile`` there is a Python
# ``UploadFile(file=BytesIO(...))`` (so the closed-file guards under
# test came from BytesIO, not from ``PyUploadFile`` / ``PySyncFile``
# at all). The Rust source is correct, but exercising it requires
# the real-loopback path (Tower / Axum router constructs the Rust
# ``PyUploadFile`` and hands it to the handler). These tests do that
# — they're skipped in sandbox via ``requires_loopback``.


pytestmark_real_loopback = pytest.mark.requires_loopback


@pytest.mark.requires_loopback
def test_rust_path_async_read_after_close_raises():
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        cls = type(file).__name__
        await file.close()
        try:
            await file.read()
        except ValueError as exc:
            return JSONResponse({"raised": True, "msg": str(exc), "cls": cls})
        return JSONResponse({"raised": False, "cls": cls})

    # Real loopback — TestClient default starts the Axum server on
    # a free port. The handler receives a Rust ``PyUploadFile`` (not
    # the in-process Python ``UploadFile`` wrapper).
    with TestClient(app) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        # Confirm we actually hit the Rust path — the Python class
        # name should be ``PyUploadFile`` (the bound Rust pyclass).
        assert body["cls"] == "PyUploadFile", body
        assert body["raised"] is True, body
        assert "closed" in body["msg"].lower(), body


@pytest.mark.requires_loopback
def test_rust_path_async_write_after_close_raises():
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        await file.close()
        try:
            await file.write(b"x")
        except ValueError:
            return JSONResponse({"raised": True})
        return JSONResponse({"raised": False})

    with TestClient(app) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json() == {"raised": True}


@pytest.mark.requires_loopback
def test_rust_path_sync_read_after_close_raises():
    """Sync ``file.file.read()`` after async ``await file.close()``
    must raise — ``PySyncFile`` shares the closed flag with
    ``PyUploadFile`` via ``Arc<Mutex<bool>>``."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        sync_handle = file.file
        sync_cls = type(sync_handle).__name__
        await file.close()
        try:
            sync_handle.read()
        except ValueError:
            return JSONResponse({"raised": True, "sync_cls": sync_cls})
        return JSONResponse({"raised": False, "sync_cls": sync_cls})

    with TestClient(app) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        body = r.json()
        # Confirm the sync handle is the Rust ``PySyncFile`` (not
        # ``BytesIO`` from the in-process Python path).
        assert body["sync_cls"] == "PySyncFile", body
        assert body["raised"] is True, body


@pytest.mark.requires_loopback
def test_rust_path_sync_write_after_close_raises():
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        sync_handle = file.file
        await file.close()
        try:
            sync_handle.write(b"x")
        except ValueError:
            return JSONResponse({"raised": True})
        return JSONResponse({"raised": False})

    with TestClient(app) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json() == {"raised": True}
