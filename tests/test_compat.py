"""Compat-shim pins NOT covered by the upstream suite or the shim matrix.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * all same-name import tautologies (``from fastapi import X`` /
    ``from fastapi import X as Y``; FastAPI, Depends, HTTPException,
    Query/Path/Header/Cookie/Body/Form/File, JSONResponse/HTMLResponse/
    Response, Request, UploadFile, APIRouter, BackgroundTasks, WebSocket,
    responses/routing/exceptions/encoders/testclient modules)
                                → tests/test_shim_completeness.py (pkgutil
                                  walk + DOOR_CRITICAL identity matrix);
                                  every name grep-confirmed imported by the
                                  upstream 0.138.1 suite
  * starlette.responses ≡ fastapi.responses identity
                                → DOOR_CRITICAL matrix rows + upstream
                                  ``from starlette.responses import ...``
                                  (tests/test_exception_handlers.py)
  * fastapi/starlette CORS identity
                                → DOOR_CRITICAL rows (both paths map to
                                  fastapi_turbo.middleware.cors)
  * concurrency / background / websockets / datastructures import smokes
                                → DOOR_CRITICAL rows + module walk
  * security class surface (HTTPBasicCredentials, SecurityScopes, ...)
                                → tests/test_security_http_basic_realm.py,
                                  tests/test_security_http_bearer.py,
                                  upstream SecurityScopes suites
  * fastapi/starlette status constants
                                → upstream usage pins (tests/test_ws_router.py
                                  asserts WS_1000/WS_1008 values; HTTP_*
                                  asserted across the whole upstream suite)
"""

import fastapi_turbo  # noqa: F401 — ensure shims are installed


def test_starlette_exceptions_import():
    """Patch-on-real model: ``fastapi.exceptions.HTTPException`` is the real
    FastAPI subclass of the real Starlette ``HTTPException`` (genuine
    upstream relationship, no shim collapsing)."""
    from starlette.exceptions import HTTPException
    from fastapi.exceptions import HTTPException as FastAPIHTTPException

    assert issubclass(FastAPIHTTPException, HTTPException)
    # The turbo engine catches the Starlette BASE class, so both flavors
    # land in the same handler machinery.
    import fastapi_turbo
    assert fastapi_turbo.HTTPException is HTTPException


def test_shim_uninstall_reinstall():
    """Patch-on-real model: uninstall restores the pristine real-package
    attributes (the module stays importable); install re-patches."""
    import sys
    import fastapi_turbo
    from fastapi_turbo.compat import uninstall, install

    # The REAL package stays live in sys.modules; only attributes differ.
    assert "fastapi" in sys.modules
    real_fastapi = sys.modules["fastapi"]
    assert not isinstance(real_fastapi, type(fastapi_turbo)) or hasattr(
        real_fastapi, "__file__"
    )
    assert real_fastapi.FastAPI is fastapi_turbo.FastAPI

    uninstall()
    try:
        # Module still importable and back to the genuine class.
        assert "fastapi" in sys.modules
        assert sys.modules["fastapi"] is real_fastapi
        assert real_fastapi.FastAPI is not fastapi_turbo.FastAPI
        assert issubclass(fastapi_turbo.FastAPI, real_fastapi.FastAPI)
        # Mirror-loop extensions are removed again.
        assert not hasattr(real_fastapi, "fastapi_turbo_version")
    finally:
        install()

    assert real_fastapi.FastAPI is fastapi_turbo.FastAPI
    assert real_fastapi.fastapi_turbo_version == fastapi_turbo.__version__

    # Verify imports still work
    from fastapi import FastAPI
    from fastapi import FastAPI as JF
    assert FastAPI is JF
    assert FastAPI is fastapi_turbo.FastAPI
