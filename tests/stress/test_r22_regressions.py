"""R22 audit follow-ups — closed-file I/O guards on Rust
``PyUploadFile`` / ``PySyncFile``, ``Request.close`` actually closes
the parsed form, and ``FormData.close`` is strict (UploadFile-only,
errors propagate)."""
import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 closed Rust UploadFile rejects further reads/writes/seeks
# ────────────────────────────────────────────────────────────────────


def test_rust_uploadfile_async_read_after_close_raises():
    """``await upload.read()`` after ``await upload.close()`` must
    raise ``ValueError("I/O operation on closed file.")`` —
    Starlette's ``UploadFile`` inherits this from
    ``SpooledTemporaryFile``. Earlier Rust impl only flipped the
    ``closed`` flag and let reads keep returning bytes."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        await file.close()
        try:
            await file.read()
        except ValueError as exc:
            return JSONResponse({"raised": True, "msg": str(exc)})
        return JSONResponse({"raised": False})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        assert body["raised"] is True, body
        assert "closed" in body["msg"].lower(), body


def test_rust_uploadfile_async_write_after_close_raises():
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

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"raised": True}


def test_rust_uploadfile_async_seek_after_close_raises():
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        await file.close()
        try:
            await file.seek(0)
        except ValueError:
            return JSONResponse({"raised": True})
        return JSONResponse({"raised": False})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json() == {"raised": True}


def test_rust_uploadfile_sync_read_after_close_raises():
    """Sync ``upload.file.read()`` must also raise after close —
    ``PySyncFile`` shares the closed flag with ``PyUploadFile``."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        await file.close()
        try:
            file.file.read()
        except ValueError:
            return JSONResponse({"raised": True})
        return JSONResponse({"raised": False})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json() == {"raised": True}


def test_rust_uploadfile_close_idempotent():
    """Calling ``close`` twice must NOT raise (idempotent, matches
    ``io.IOBase.close``)."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        await file.close()
        await file.close()
        # ``UploadFile.file`` (BytesIO in the in-process Python path,
        # ``PySyncFile`` in the Rust path) is the canonical place
        # ``.closed`` lives — it mirrors ``SpooledTemporaryFile``.
        return JSONResponse({"closed": file.file.closed})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json() == {"closed": True}


# ────────────────────────────────────────────────────────────────────
# #2 Request.close() actually closes the parsed form
# ────────────────────────────────────────────────────────────────────


def test_request_close_closes_parsed_form_uploads():
    """``await request.close()`` must close every ``UploadFile`` in
    the previously-parsed form. Earlier impl was a bare ``pass`` —
    upload buffers stayed open until GC. Probe-confirmed parity:
    upstream toggled ``upload.file.closed`` to ``True`` after
    request.close; we did not."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(request: Request):
        form = await request.form()
        f = form.get("file")
        before = f.file.closed if f is not None else None
        await request.close()
        after = f.file.closed if f is not None else None
        return JSONResponse({"before": before, "after": after})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"before": False, "after": True}


def test_request_close_noop_when_no_form_parsed():
    """``request.close`` over a request whose body was never parsed
    as a form must be a clean no-op (no exceptions, no side-effects).
    Matches Starlette."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/x")
    async def _x(request: Request):
        await request.close()
        return JSONResponse({"ok": True})

    with TestClient(app, in_process=True) as c:
        r = c.get("/x")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# ────────────────────────────────────────────────────────────────────
# #3 FormData.close() is strict — UploadFile-only, errors propagate
# ────────────────────────────────────────────────────────────────────


def test_formdata_close_skips_non_uploadfile_values():
    """A dummy value with a ``close`` attribute must NOT have its
    ``close`` called by ``FormData.close()``. Starlette only closes
    ``UploadFile`` instances. Earlier impl was duck-typed (any value
    with a ``close`` attr got closed) and silently swallowed errors,
    which probes against upstream caught."""
    import asyncio

    async def _run() -> dict:
        from fastapi_turbo.datastructures import FormData

        called = {"dummy_close": 0, "str_close": 0}

        class _Dummy:
            def close(self):
                called["dummy_close"] += 1

        # Strings don't have ``close``; this just exercises the
        # plain-value path for completeness.
        f = FormData([
            ("a", "text"),
            ("dummy", _Dummy()),
        ])
        await f.close()
        return called

    result = asyncio.run(_run())
    assert result == {"dummy_close": 0, "str_close": 0}, result


def test_formdata_close_propagates_uploadfile_close_errors():
    """If an ``UploadFile.close`` raises, ``FormData.close`` must
    propagate the exception — cleanup failures are load-bearing.
    Earlier impl swallowed everything in ``try/except``."""
    import asyncio

    async def _run() -> str:
        from fastapi_turbo import UploadFile
        from fastapi_turbo.datastructures import FormData

        class _BadUpload(UploadFile):
            def __init__(self):
                super().__init__(filename="x", file=None)

            async def close(self):
                raise OSError("boom")

        f = FormData([("file", _BadUpload())])
        try:
            await f.close()
        except OSError as exc:
            return str(exc)
        return ""

    assert asyncio.run(_run()) == "boom"


def test_formdata_close_text_fields_only_is_clean_noop():
    """Regression guard for R21: ``FormData.close`` over a form with
    only text fields must run without error. The strict-mode change
    from R22 still permits this (no UploadFile values means no
    closes attempted)."""
    import asyncio

    async def _run() -> bool:
        from fastapi_turbo.datastructures import FormData
        f = FormData([("a", "1"), ("b", "2")])
        await f.close()
        return True

    assert asyncio.run(_run()) is True
