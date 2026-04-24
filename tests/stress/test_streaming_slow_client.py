"""Regression: streaming must not stall other Python threads when the
HTTP client drains the body slowly.

Previously ``tx.blocking_send(chunk)`` ran inside ``Python::attach(|py|)``,
holding the GIL through the blocking channel send. A slow client could
pin the interpreter and stall unrelated Python threads for the duration
of backpressure.
"""
from __future__ import annotations

import threading
import time

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient


def test_slow_client_does_not_block_other_threads():
    app = FastAPI()

    def gen():
        for i in range(50):
            yield ("x" * 4096 + "\n").encode()

    @app.get("/stream")
    def stream():
        return StreamingResponse(gen(), media_type="text/plain")

    # This side-thread does CPU-bound Python work in a tight loop. If the
    # streaming handler is holding the GIL while blocked on send, this
    # thread will make no progress.
    side_counter = {"v": 0}
    stop = threading.Event()

    def side_worker():
        while not stop.is_set():
            # Small CPU work that requires the GIL.
            for _ in range(100):
                pass
            side_counter["v"] += 1

    t = threading.Thread(target=side_worker, daemon=True)
    t.start()

    try:
        with TestClient(app) as cli:
            # Use stream=True with manual iteration + sleeps to simulate
            # a slow consumer. httpx holds the connection; the server's
            # streaming task fills the channel then blocks.
            start = time.monotonic()
            before = side_counter["v"]
            with cli.stream("GET", "/stream") as r:
                chunks_read = 0
                for chunk in r.iter_bytes(chunk_size=1):
                    chunks_read += 1
                    if chunks_read > 4:
                        # Enough to force backpressure on the server.
                        time.sleep(0.1)
                    if chunks_read > 10:
                        break
            elapsed = time.monotonic() - start
            after = side_counter["v"]
    finally:
        stop.set()
        t.join(timeout=1)

    # The side thread should have made meaningful progress during the
    # slow drain. Empirically in a healthy run we see counter increments
    # well into the hundreds/thousands. Setting the floor low enough to
    # avoid CI flakiness but high enough to catch full GIL-holding.
    progress = after - before
    assert progress > 10, f"side thread stalled: {progress} ticks in {elapsed:.2f}s"
