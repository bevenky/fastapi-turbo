"""Exception ``__traceback__`` provenance across the Rust door.

``TestClient(raise_server_exceptions=True)`` re-raises server-side
exceptions in the test thread (``testclient._check_raised`` →
``raise exc``). A bare ``raise exc`` preserves ``exc.__traceback__`` —
so the DEEPEST frame the caller sees must still be the user's failing
line, not turbo's re-raise site.

On Python <3.12 PyO3 stores a raised error as the legacy ``(type,
value, traceback)`` triple (``PyErr_Fetch``): the traceback rides in a
separate slot and ``PyErr::value()`` hands Python an exception object
with ``__traceback__ = None``. Every door boundary that used
``e.value(py)`` to capture the exception (``_captured_server_
exceptions`` appends, ``_door_handle_dep_exception``) therefore lost
frame provenance on 3.10/3.11 — the re-raise then fabricated a
traceback rooted inside ``testclient.py``. On 3.12+
``PyErr_GetRaisedException`` already carries the traceback, which is
why only the old runtimes regressed (upstream's
``test_exception_handlers.py::test_traceback_for_dependency_with_yield``
was version-deselected for exactly this).

Fixed by ``src/responses.rs::err_value_with_tb`` — attaches
``err.traceback(py)`` to the value before it crosses to Python. These
tests pin the contract on ALL runtimes so the 3.10/3.11 CI legs enforce
it forever.
"""
from __future__ import annotations

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


def _raise_value_error():
    raise ValueError("provenance boom")


def _deepest(exc: BaseException):
    tb = exc.__traceback__
    assert tb is not None, "exception crossed the door with __traceback__=None"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb


def _assert_original_frame(exc: BaseException) -> None:
    """The deepest traceback frame must be the ``raise`` line inside
    ``_raise_value_error`` — not a turbo re-raise site."""
    tb = _deepest(exc)
    assert tb.tb_frame.f_code.co_filename == __file__, (
        f"deepest frame is {tb.tb_frame.f_code.co_filename}:{tb.tb_lineno} "
        f"in {tb.tb_frame.f_code.co_name} — original traceback was dropped"
    )
    assert tb.tb_frame.f_code.co_name == "_raise_value_error"
    # ``raise`` is the first statement of the function body.
    assert tb.tb_lineno == _raise_value_error.__code__.co_firstlineno + 1


def test_dep_yield_setup_raise_with_exception_handler_keeps_frames():
    """Upstream's ``test_traceback_for_dependency_with_yield`` shape: a
    yield-dep raising at setup while a bare ``Exception`` handler is
    registered routes through ``_door_handle_dep_exception`` (Rust hands
    the exception OBJECT to Python) — the path that lost provenance on
    3.10/3.11."""

    def server_error_handler(request, exception):
        return JSONResponse(status_code=500, content={"exception": "server-error"})

    app = FastAPI(exception_handlers={Exception: server_error_handler})

    def dep():
        yield _raise_value_error()

    @app.get("/d", dependencies=[Depends(dep)])
    def handler(): ...

    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(ValueError) as exc_info:
        client.get("/d")
    _assert_original_frame(exc_info.value)


def test_dep_yield_setup_raise_no_handler_keeps_frames():
    """Same raise without user handlers → Rust default-500 path
    (``pyerr_to_response`` capture onto ``_captured_server_exceptions``)."""
    app = FastAPI()

    def dep():
        yield _raise_value_error()

    @app.get("/d", dependencies=[Depends(dep)])
    def handler(): ...

    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(ValueError) as exc_info:
        client.get("/d")
    _assert_original_frame(exc_info.value)


def test_dep_yield_teardown_raise_keeps_frames():
    """Raise AFTER the yield (real teardown): the response is already
    sent, ``teardown_request_scope_gens`` captures the exception for the
    TestClient re-raise — provenance must survive that crossing too."""
    app = FastAPI()

    def dep():
        yield "x"
        _raise_value_error()

    @app.get("/t", dependencies=[Depends(dep)])
    def handler():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(ValueError) as exc_info:
        client.get("/t")
    _assert_original_frame(exc_info.value)


def test_handler_body_raise_keeps_frames():
    """Plain handler-body raise — the classic ``raise_server_exceptions``
    surface — keeps the user's frame as the deepest entry."""
    app = FastAPI()

    @app.get("/boom")
    def handler():
        _raise_value_error()

    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(ValueError) as exc_info:
        client.get("/boom")
    _assert_original_frame(exc_info.value)
