"""In-process ASGI must parse multipart/form-data and urlencoded
bodies, populating ``Form(...)`` / ``File(...)`` / ``UploadFile``
parameters without a loopback."""
from __future__ import annotations

import asyncio
import io

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to the loopback proxy — form/file "
            "parsing didn't run in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_urlencoded_form_fields():
    app = FastAPI()

    @app.post("/login")
    def _login(username: str = Form(...), password: str = Form(...)):
        return {"u": username, "p": password}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.post(
                "/login",
                data={"username": "alice", "password": "hunter2"},
            )
            assert r.status_code == 200
            assert r.json() == {"u": "alice", "p": "hunter2"}

    _run(go())


def test_multipart_file_upload():
    app = FastAPI()

    @app.post("/upload")
    async def _upload(f: UploadFile = File(...)):
        content = await f.read()
        return {"name": f.filename, "len": len(content)}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            files = {"f": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
            r = await cli.post("/upload", files=files)
            assert r.status_code == 200
            assert r.json() == {"name": "hello.txt", "len": 11}

    _run(go())


def test_mixed_form_and_file():
    app = FastAPI()

    @app.post("/submit")
    async def _submit(
        title: str = Form(...),
        f: UploadFile = File(...),
    ):
        content = await f.read()
        return {"title": title, "upload_len": len(content)}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.post(
                "/submit",
                data={"title": "cool"},
                files={"f": ("a.bin", io.BytesIO(b"xxx"), "application/octet-stream")},
            )
            assert r.status_code == 200
            assert r.json() == {"title": "cool", "upload_len": 3}

    _run(go())
