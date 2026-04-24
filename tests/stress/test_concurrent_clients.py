"""Regression: the server must serve 100 concurrent clients without
deadlock, response corruption, or unbounded latency growth.

This is the simplest smoke test for "did we accidentally introduce a
GIL-holding hot path, a lock held across a blocking syscall, or a
single shared mutable used from multiple request threads?"
"""
from __future__ import annotations

import concurrent.futures
import threading

import httpx

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_hundred_concurrent_clients_each_get_correct_response():
    app = FastAPI()

    @app.get("/echo/{n}")
    def echo(n: int):
        return {"n": n}

    cli = TestClient(app)
    # Prime the server
    cli.get("/echo/0").raise_for_status()
    base_url = f"http://127.0.0.1:{cli._port}"

    errors: list = []
    lock = threading.Lock()

    def one_client(i: int):
        try:
            with httpx.Client(base_url=base_url, timeout=10.0) as c:
                r = c.get(f"/echo/{i}")
                r.raise_for_status()
                body = r.json()
                if body != {"n": i}:
                    with lock:
                        errors.append(f"client {i}: got {body!r}")
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"client {i}: {type(e).__name__}: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
        list(ex.map(one_client, range(100)))

    assert not errors, "\n".join(errors[:10])
