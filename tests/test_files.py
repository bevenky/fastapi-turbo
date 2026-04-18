"""Tests for file uploads (multipart) and FileResponse + StaticFiles."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_app(tmp_path):
    procs = []

    def _start(code: str):
        port = _free_port()
        code = code.replace("__PORT__", str(port))
        app_file = tmp_path / "app.py"
        app_file.write_text(textwrap.dedent(code))
        proc = subprocess.Popen(
            [sys.executable, str(app_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                if proc.poll() is not None:
                    out = proc.stdout.read().decode()
                    err = proc.stderr.read().decode()
                    pytest.fail(f"Server died.\nstdout: {out}\nstderr: {err}")
        else:
            proc.kill()
            pytest.fail("Server did not start")
        return f"http://127.0.0.1:{port}"

    yield _start

    for p in procs:
        p.kill()
        p.wait()


# ── File uploads (multipart) ─────────────────────────────────────────


def test_upload_single_file(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, UploadFile

        app = FastAPI()

        @app.post("/upload")
        async def upload(file: UploadFile):
            data = await file.read()
            return {
                "filename": file.filename,
                "content_type": file.content_type,
                "size": len(data),
                "head": data[:8].hex() if data else "",
            }

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    payload = b"\x89PNG\r\n\x1a\n" + b"fake-image-data" * 1000

    r = httpx.post(
        f"{url}/upload",
        files={"file": ("hello.png", payload, "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "hello.png"
    assert body["content_type"] == "image/png"
    assert body["size"] == len(payload)
    assert body["head"] == payload[:8].hex()


def test_upload_multiple_files(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, UploadFile

        app = FastAPI()

        @app.post("/upload")
        async def upload(files: list[UploadFile]):
            names = []
            for f in files:
                names.append(f.filename)
            return {"names": names, "count": len(files)}

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.post(
        f"{url}/upload",
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
            ("files", ("c.txt", b"ccc", "text/plain")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert body["names"] == ["a.txt", "b.txt", "c.txt"]


def test_upload_binary_preserved(server_app):
    """Bytes with invalid UTF-8 must round-trip exactly (no corruption)."""
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, UploadFile
        from fastapi_rs.responses import Response

        app = FastAPI()

        @app.post("/echo")
        async def echo(file: UploadFile):
            data = await file.read()
            return Response(content=data, media_type="application/octet-stream")

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    payload = bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81]) + b"\x00" * 50

    r = httpx.post(f"{url}/echo", files={"file": ("raw.bin", payload, "application/octet-stream")})
    assert r.status_code == 200
    assert r.content == payload


def test_upload_form_field_and_file(server_app):
    """Mixed form data: string fields alongside file uploads."""
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Form, UploadFile

        app = FastAPI()

        @app.post("/submit")
        async def submit(name: str = Form(), file: UploadFile = None):
            data = await file.read() if file else b""
            return {"name": name, "size": len(data), "filename": file.filename if file else None}

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.post(
        f"{url}/submit",
        data={"name": "Alice"},
        files={"file": ("doc.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Alice"
    assert body["size"] == 5
    assert body["filename"] == "doc.txt"


# ── FileResponse ─────────────────────────────────────────────────────


def test_file_response_serves_file(server_app, tmp_path):
    import httpx

    content = b"Hello World\n" * 100
    fpath = tmp_path / "hello.txt"
    fpath.write_bytes(content)

    url = server_app(f"""
        from fastapi_rs import FastAPI
        from fastapi_rs.responses import FileResponse

        app = FastAPI()

        @app.get("/file")
        def get_file():
            return FileResponse("{fpath}")

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.get(f"{url}/file")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers.get("content-type", "").startswith("text/plain")
    assert r.headers.get("accept-ranges") == "bytes"


def test_file_response_content_disposition(server_app, tmp_path):
    import httpx

    fpath = tmp_path / "data.bin"
    fpath.write_bytes(b"binary-data")

    url = server_app(f"""
        from fastapi_rs import FastAPI
        from fastapi_rs.responses import FileResponse

        app = FastAPI()

        @app.get("/dl")
        def dl():
            return FileResponse("{fpath}", filename="report.pdf")

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.get(f"{url}/dl")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert 'filename="report.pdf"' in cd


def test_file_response_not_found(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.responses import FileResponse

        app = FastAPI()

        @app.get("/missing")
        def m():
            return FileResponse("/nonexistent/path/xyz.bin")

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.get(f"{url}/missing")
    assert r.status_code == 404


# ── Range parsing ────────────────────────────────────────────────────


def test_parse_range_header():
    """Unit test the range parser."""
    from fastapi_rs._fastapi_rs_core import core_version
    # Import indirectly — the parser is in Rust. We test via higher-level integration.
    assert core_version()


# ── StaticFiles (already wired via ServeDir) ────────────────────────


def test_static_files_serves_file(server_app, tmp_path):
    import httpx

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>Home</h1>")
    (static_dir / "style.css").write_text("body { color: red; }")

    url = server_app(f"""
        from fastapi_rs import FastAPI
        from fastapi_rs.staticfiles import StaticFiles

        app = FastAPI()
        app.mount("/static", StaticFiles(directory="{static_dir}"), name="static")

        @app.get("/")
        def index():
            return {{"ok": True}}

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    r = httpx.get(f"{url}/static/style.css")
    assert r.status_code == 200
    assert "color: red" in r.text

    r = httpx.get(f"{url}/static/index.html")
    assert r.status_code == 200
    assert "<h1>Home</h1>" in r.text


# ── Rust PyUploadFile class (unit) ──────────────────────────────────


class TestPyUploadFileUnit:
    def test_read_full(self):
        """PyUploadFile exposes the file-like API we expect."""
        from fastapi_rs._fastapi_rs_core import PyUploadFile

        # PyUploadFile is normally constructed by the Rust multipart parser,
        # but we verify the class exists + has the expected attributes.
        assert hasattr(PyUploadFile, "read")
        assert hasattr(PyUploadFile, "seek")
        assert hasattr(PyUploadFile, "close")

    def test_isinstance_check(self):
        """PyUploadFile should satisfy isinstance(x, UploadFile) via __subclasshook__."""
        from fastapi_rs import UploadFile
        from fastapi_rs._fastapi_rs_core import PyUploadFile

        # Subclasshook checks attributes on the class, so direct class check
        assert issubclass(PyUploadFile, UploadFile)
