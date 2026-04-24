"""FileResponse ``filename`` → Content-Disposition encoding parity.

Starlette URL-quotes the filename; if quoting actually changed the
string (non-ASCII bytes, spaces, CJK, control chars, etc.) it emits the
RFC 5987 ``filename*=utf-8''<quoted>`` form. ASCII-safe filenames get
the plain ``filename="..."`` form.

The previous implementation wrote ``filename="<raw>"`` for everything,
which corrupts CJK filenames on most clients and breaks strict HTTP
parsers."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def _write(tmp, name, content):
    f = tmp / name
    f.write_bytes(content)
    return f


def test_ascii_filename_uses_plain_form(tmp_path):
    f = _write(tmp_path, "src.bin", b"hello")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f), filename="report.pdf")

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert cd == 'attachment; filename="report.pdf"', cd


def test_non_ascii_filename_uses_rfc5987_form(tmp_path):
    f = _write(tmp_path, "src.bin", b"hello")
    app = FastAPI()

    @app.get("/f")
    def _f():
        # Cyrillic characters — raw form would emit invalid header bytes.
        return FileResponse(str(f), filename="отчёт.pdf")

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        # Must use RFC 5987 extended form.
        assert "filename*=utf-8''" in cd, f"expected filename* form, got: {cd}"
        # Must NOT also contain the raw non-ASCII bytes in a plain filename=.
        assert "filename=\"отчёт" not in cd


def test_filename_with_spaces_uses_rfc5987_form(tmp_path):
    f = _write(tmp_path, "src.bin", b"hello")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f), filename="my report.pdf")

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        # Space → URL-quoted in RFC 5987 form ("my%20report.pdf").
        assert "filename*=utf-8''my%20report.pdf" in cd, cd


def test_media_type_inferred_from_filename_when_path_lacks_extension(tmp_path):
    """Starlette: when ``filename`` is provided, its extension is the
    first source for the media_type guess (falling back to the path)."""
    f = tmp_path / "payload"  # extensionless
    f.write_bytes(b"<!doctype html><p>hi</p>")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f), filename="page.html")

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        # Should be text/html (inferred from ``filename``), not
        # application/octet-stream (what path alone would guess).
        assert r.headers["content-type"].startswith("text/html"), (
            r.headers["content-type"]
        )
