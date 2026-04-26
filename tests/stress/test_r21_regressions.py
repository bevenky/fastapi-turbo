"""R21 audit follow-ups — Starlette parity for ``UploadFile.size``
counter, case-insensitive multipart MIME parameter names,
``Request.form()`` no-content-type behaviour, and async
``FormData.close()``."""
import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 UploadFile.size tracked separately from buffer length
# ────────────────────────────────────────────────────────────────────


def test_uploadfile_size_unchanged_by_sync_file_write():
    """``upload.file.write(...)`` mutates the underlying buffer but
    does NOT increment ``upload.size`` — Starlette's
    ``SpooledTemporaryFile.write`` is opaque to ``UploadFile.size``
    bookkeeping. Earlier Rust impl returned ``data.len()`` from the
    ``size`` getter, so any sync write that grew the buffer made
    ``upload.size`` diverge from upstream."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        before = file.size
        # Sync write through the file-like — bytes land in the buffer
        # but ``size`` must NOT move.
        file.file.write(b"X")
        after = file.size
        return JSONResponse({"before": before, "after": after})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        # 3-byte upload → size=3; sync write of 1 byte → size still 3.
        assert body == {"before": 3, "after": 3}, body


def test_uploadfile_size_incremented_by_async_write_overwrite():
    """``await upload.write(b)`` increments ``upload.size`` by
    ``len(b)`` regardless of cursor position — even when the write
    overlays existing bytes (cursor=0, len(b) ≤ existing data).
    Starlette's ``UploadFile.write`` does ``self.size += len(data)``
    unconditionally; earlier Rust impl reported ``data.len()`` so an
    overwrite that didn't grow the buffer left ``size`` unchanged."""
    from fastapi_turbo import FastAPI, UploadFile
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(file: UploadFile):
        before = file.size
        await file.seek(0)
        # Overwrite — buffer length unchanged, but size must grow.
        await file.write(b"XY")  # len 2
        after = file.size
        return JSONResponse({"before": before, "after": after})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        # 3-byte upload → size=3; async write of 2 bytes (overlaying
        # cursor=0..2) → size=5 (3 + 2).
        assert body == {"before": 3, "after": 5}, body


# ────────────────────────────────────────────────────────────────────
# #2 multipart MIME parameter names case-insensitive
# ────────────────────────────────────────────────────────────────────


def test_request_form_accepts_capital_boundary_param():
    """``Content-Type: multipart/form-data; Boundary=AaB03x`` — RFC
    2045 §5.1 permits ``Boundary`` (param names are case-insensitive)
    while keeping the *value* case-sensitive. Earlier code matched
    only ``boundary=`` and yielded an empty ``FormData`` for the
    capitalised form."""
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
                "content-type": f"multipart/form-data; Boundary={boundary}",
            },
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"hello": "world"}, r.json()


def test_request_form_accepts_capital_disposition_param_names():
    """``Content-Disposition: form-data; Name="x"`` — capitalised
    parameter name still resolves to the field. Earlier code matched
    only the lowercase ``name`` key, so capitalised dispositions
    surfaced as parts with no name and got dropped."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    boundary = "BoUnDaRy"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; Name="hello"\r\n\r\n'
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
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"hello": "world"}, r.json()


def test_form_endpoint_accepts_capital_disposition_param_names():
    """The in-app dispatcher (``Form(...)`` injection path) also
    lowercases parameter names. Capital ``Name="x"`` must reach the
    endpoint as the named field — earlier code returned 422 because
    the dispatcher saw a part with no ``name`` parameter and dropped
    it."""
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/f")
    async def _f(hello: str = Form(...)):
        return {"hello": hello}

    boundary = "BoUnDaRy"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; Name="hello"\r\n\r\n'
        "world\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/f",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"hello": "world"}, r.json()


# ────────────────────────────────────────────────────────────────────
# #3 request.form() with no Content-Type returns empty FormData
# ────────────────────────────────────────────────────────────────────


def test_request_form_returns_empty_when_no_content_type():
    """Posting raw bytes with no ``Content-Type`` header must NOT
    speculatively run ``parse_qsl`` — Starlette returns an empty
    ``FormData`` in that case. Earlier code had ``or not ct_lower``
    fallback that surfaced arbitrary bytes as fake ``(key, value)``
    pairs."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(request: Request):
        form = await request.form()
        return JSONResponse({"items": list(form.multi_items())})

    with TestClient(app, in_process=True) as c:
        # Force an empty Content-Type so ``request.headers.get
        # ('content-type')`` returns ``""``. ``content=`` with no
        # explicit header passes httpx through ``application/octet-
        # stream`` defaults, which still aren't urlencoded — but the
        # explicit empty header proves the absence-of-header branch.
        r = c.post(
            "/u",
            content=b"a=1&b=2",
            headers={"content-type": ""},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"items": []}, r.json()


# ────────────────────────────────────────────────────────────────────
# #4 FormData.close() awaits each UploadFile.close()
# ────────────────────────────────────────────────────────────────────


def test_formdata_close_releases_upload_files():
    """``await form.close()`` must call ``close()`` on every
    contained ``UploadFile`` so its underlying file handle releases.
    Earlier ``FormData`` had no ``close`` method — a drop-in API
    surface gap caught by upstream tests that ``await
    form.close()`` in a ``finally``."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/u")
    async def _u(request: Request):
        form = await request.form()
        f = form.get("file")
        before = bool(getattr(f.file, "closed", False)) if f is not None else None
        await form.close()
        after = bool(getattr(f.file, "closed", False)) if f is not None else None
        return JSONResponse({"before": before, "after": after})

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            files={"file": ("a.txt", b"abc", "text/plain")},
        )
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        assert body == {"before": False, "after": True}, body


def test_formdata_close_idempotent_with_text_fields():
    """``form.close()`` over a form with text fields (no ``close``
    method) must not raise. ``close`` is awaited only on values that
    expose one."""
    import asyncio

    async def _run() -> bool:
        from fastapi_turbo.datastructures import FormData

        f = FormData([("a", "1"), ("b", "2")])
        await f.close()
        return True

    assert asyncio.run(_run()) is True
