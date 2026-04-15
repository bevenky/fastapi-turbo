"""TestClient for testing fastapi-rs/FastAPI applications.

Starts the Rust-backed server in a background thread and provides
an httpx-based client for making real HTTP requests.

Usage::

    from fastapi_rs import FastAPI
    from fastapi_rs.testclient import TestClient

    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    with TestClient(app) as client:
        r = client.get("/hello")
        assert r.status_code == 200
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx


class TestClient:
    """HTTP test client that launches a real fastapi-rs server in a background thread."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
    ):
        self.app = app
        self._base_url = base_url
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._raise_server_exceptions = raise_server_exceptions

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def __enter__(self) -> TestClient:
        self._port = self._find_free_port()
        self._thread = threading.Thread(
            target=self.app.run,
            kwargs={"host": "127.0.0.1", "port": self._port},
            daemon=True,
        )
        self._thread.start()

        # Wait for server to be ready
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError("TestClient: server did not start within 10 seconds")

        self._client = httpx.Client(base_url=f"http://127.0.0.1:{self._port}")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ── HTTP verb shortcuts ────────────────────────────────────────

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.put(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.delete(url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.patch(url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.options(url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.head(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.request(method, url, **kwargs)
