"""``FastAPI()`` bound to a public address (0.0.0.0 / ::) without a
``max_request_size`` cap is a DoS footgun — a single client can stream
an arbitrary body to OOM the worker. We emit a ``UserWarning`` so the
operator sees it at startup.

Exit conditions:
  * public bind + no cap → warns
  * loopback bind → never warns
  * public bind + cap set → no warning
  * ``FASTAPI_TURBO_SUPPRESS_DOS_WARNING=1`` → no warning
"""
from __future__ import annotations

import socket
import threading
import time
import warnings

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run_and_capture_warnings(host: str, cap: int | None, env: dict | None = None):
    import os

    original_env = {}
    if env:
        for k, v in env.items():
            original_env[k] = os.environ.get(k)
            os.environ[k] = v
    try:
        app = FastAPI(max_request_size=cap)
        port = _free_port()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            t = threading.Thread(
                target=lambda: app.run(host=host, port=port), daemon=True
            )
            t.start()
            time.sleep(0.4)
            return [str(w.message) for w in caught]
    finally:
        if env:
            for k, prev in original_env.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev


def test_public_bind_without_cap_warns():
    msgs = _run_and_capture_warnings(host="0.0.0.0", cap=None)
    assert any("max_request_size" in m for m in msgs), msgs


def test_loopback_bind_never_warns():
    msgs = _run_and_capture_warnings(host="127.0.0.1", cap=None)
    assert not any("max_request_size" in m for m in msgs), msgs


def test_public_bind_with_cap_does_not_warn():
    msgs = _run_and_capture_warnings(host="0.0.0.0", cap=1024 * 1024)
    assert not any("max_request_size" in m for m in msgs), msgs


def test_suppress_env_var_silences_warning():
    msgs = _run_and_capture_warnings(
        host="0.0.0.0", cap=None, env={"FASTAPI_TURBO_SUPPRESS_DOS_WARNING": "1"}
    )
    assert not any("max_request_size" in m for m in msgs), msgs
