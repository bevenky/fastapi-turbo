"""``PyPool`` must surface a clean ``RuntimeError`` when constructed
outside a Tokio runtime rather than unwinding a Rust panic across the
FFI boundary (which abort()s the Python process on some platforms)."""
from __future__ import annotations

import pytest

import fastapi_turbo  # noqa: F401


def test_pypool_outside_runtime_raises_runtime_error():
    from fastapi_turbo._fastapi_turbo_core import PyPool

    with pytest.raises(RuntimeError) as excinfo:
        PyPool("postgres://localhost/fakedb_unused")

    msg = str(excinfo.value)
    assert "Tokio runtime" in msg or "tokio" in msg.lower()
