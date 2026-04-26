"""Phase 7 tests: StreamingResponse support."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest




# -- Streaming response tests -----------------------------------------------


def test_streaming_response_sync_generator(server_app):
    """StreamingResponse with a sync generator."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        def generate():
            for i in range(5):
                yield f"chunk {i}\\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(generate(), media_type="text/plain")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/stream")
    assert r.status_code == 200
    for i in range(5):
        assert f"chunk {i}" in r.text


def test_streaming_response_async_generator(server_app):
    """StreamingResponse with an async generator."""
    url = server_app("""
        import asyncio
        import fastapi_turbo  # noqa: F401 — installs compat shim
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        async def generate():
            for i in range(5):
                yield f"chunk {i}\\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(generate(), media_type="text/plain")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/stream")
    assert r.status_code == 200
    for i in range(5):
        assert f"chunk {i}" in r.text


def test_streaming_response_bytes(server_app):
    """StreamingResponse yielding bytes."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        def generate():
            for i in range(3):
                yield f"byte-chunk-{i}\\n".encode()

        @app.get("/stream")
        async def stream():
            return StreamingResponse(generate(), media_type="application/octet-stream")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/stream")
    assert r.status_code == 200
    for i in range(3):
        assert f"byte-chunk-{i}" in r.text


def test_streaming_response_custom_status(server_app):
    """StreamingResponse with custom status code."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        def generate():
            yield "partial content\\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(generate(), status_code=206, media_type="text/plain")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/stream")
    assert r.status_code == 206
    assert "partial content" in r.text


def test_streaming_response_custom_headers(server_app):
    """StreamingResponse with custom headers."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        def generate():
            yield "data\\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(
                generate(),
                media_type="text/plain",
                headers={"x-custom": "test-value"},
            )

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/stream")
    assert r.status_code == 200
    assert r.headers.get("x-custom") == "test-value"


def test_streaming_with_regular_routes(server_app):
    """StreamingResponse alongside regular routes."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        app = FastAPI()

        @app.get("/hello")
        async def hello():
            return {"message": "hello"}

        def generate():
            yield "stream data\\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(generate(), media_type="text/plain")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    # Regular route
    r1 = httpx.get(f"{url}/hello")
    assert r1.status_code == 200
    assert r1.json() == {"message": "hello"}

    # Streaming route
    r2 = httpx.get(f"{url}/stream")
    assert r2.status_code == 200
    assert "stream data" in r2.text
