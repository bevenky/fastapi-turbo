"""FileResponse parity: directory paths must raise (not serve 200 empty)
and the extensionless media_type default must match Starlette
(``text/plain``, not ``application/octet-stream``).

Directory-as-file was a latent data-disclosure risk: if a routing bug
fed a dir path to FileResponse, we'd previously respond 200 with an
empty body and headers implying success — hiding the misconfiguration
from monitoring."""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _run(coro):
    return asyncio.run(coro)


def test_directory_path_does_not_return_200_empty(tmp_path):
    """Passing a directory to FileResponse must surface as an error,
    not a silent empty 200. Starlette raises ``RuntimeError`` inside
    ``__call__``; under ``httpx.ASGITransport`` with the default
    ``raise_app_exceptions=True`` the exception propagates to the
    caller. Real ASGI servers (uvicorn) convert it to 500."""
    d = tmp_path / "some_dir"
    d.mkdir()
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(d))

    async def go():
        # ``raise_app_exceptions=False`` → transport converts
        # RuntimeError to 500 so we can assert the HTTP-visible shape.
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://t",
        ) as cli:
            r = await cli.get("/f")
            assert r.status_code == 500, (
                f"directory-as-FileResponse should surface as 500, "
                f"got status={r.status_code} content={r.content!r}"
            )

    _run(go())


def test_extensionless_file_defaults_to_text_plain(tmp_path):
    """Starlette defaults ``media_type`` to ``text/plain`` for files
    whose name doesn't map to a known MIME type (``README``, shell
    scripts, config files named without extension, etc.)."""
    f = tmp_path / "README"  # no extension → mimetypes.guess_type returns None
    f.write_bytes(b"this is a readme")

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        ct = r.headers["content-type"]
        # Upstream Starlette → "text/plain" (or "text/plain; charset=utf-8"
        # after textual-charset rewriting on the Rust path).
        assert ct.startswith("text/plain"), f"expected text/plain default, got {ct}"
