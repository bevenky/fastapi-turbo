"""Phase 9-10 tests: compatibility shims for fastapi.* and starlette.* imports."""

import fastapi_turbo  # noqa: F401 — ensure shims are installed


# ── fastapi top-level imports ──────────────────────────────────────


def test_fastapi_import_fastapi():
    """import fastapi_turbo  # noqa: F401 — installs compat shim
from fastapi import FastAPI resolves to fastapi_turbo."""
    from fastapi import FastAPI
    from fastapi import FastAPI as TurboFastAPI

    assert FastAPI is TurboFastAPI


def test_fastapi_depends_import():
    from fastapi import Depends
    from fastapi import Depends as TurboDepends

    assert Depends is TurboDepends


def test_fastapi_http_exception_import():
    from fastapi import HTTPException
    from fastapi import HTTPException as TurboHTTPException

    assert HTTPException is TurboHTTPException


def test_fastapi_query_path_import():
    from fastapi import Query, Path, Header, Cookie, Body, Form, File
    from fastapi import Query as JQ, Path as JP, Header as JH
    from fastapi import Cookie as JC, Body as JB, Form as JFo, File as JFi

    assert Query is JQ
    assert Path is JP
    assert Header is JH
    assert Cookie is JC
    assert Body is JB
    assert Form is JFo
    assert File is JFi


def test_fastapi_response_import():
    from fastapi import JSONResponse, HTMLResponse, Response
    from fastapi import JSONResponse as JJ, HTMLResponse as JH, Response as JR

    assert JSONResponse is JJ
    assert HTMLResponse is JH
    assert Response is JR


def test_fastapi_request_import():
    from fastapi import Request
    from fastapi import Request as TurboRequest

    assert Request is TurboRequest


def test_fastapi_uploadfile_import():
    from fastapi import UploadFile
    from fastapi import UploadFile as TurboUploadFile

    assert UploadFile is TurboUploadFile


def test_fastapi_apirouter_import():
    from fastapi import APIRouter
    from fastapi import APIRouter as TurboAPIRouter

    assert APIRouter is TurboAPIRouter


def test_fastapi_background_tasks_import():
    from fastapi import BackgroundTasks
    from fastapi import BackgroundTasks as TurboBT

    assert BackgroundTasks is TurboBT


def test_fastapi_websocket_import():
    from fastapi import WebSocket
    from fastapi import WebSocket as TurboWS

    assert WebSocket is TurboWS


# ── fastapi submodule imports ──────────────────────────────────────


def test_fastapi_responses_module():
    from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
    from fastapi.responses import JSONResponse as JJ
    from fastapi.responses import HTMLResponse as JH
    from fastapi.responses import PlainTextResponse as JP

    assert JSONResponse is JJ
    assert HTMLResponse is JH
    assert PlainTextResponse is JP


def test_fastapi_routing_module():
    from fastapi.routing import APIRouter, APIRoute
    from fastapi.routing import APIRouter as JR, APIRoute as JA

    assert APIRouter is JR
    assert APIRoute is JA


def test_fastapi_exceptions_module():
    from fastapi.exceptions import HTTPException, RequestValidationError
    from fastapi.exceptions import HTTPException as JH, RequestValidationError as JR

    assert HTTPException is JH
    assert RequestValidationError is JR


def test_fastapi_security_import():
    from fastapi.security import OAuth2PasswordBearer
    from fastapi.security import OAuth2PasswordBearer as TurboOAuth2

    assert OAuth2PasswordBearer is TurboOAuth2


def test_fastapi_security_all_classes():
    from fastapi.security import (
        OAuth2PasswordBearer,
        HTTPBearer,
        HTTPBasic,
        APIKeyHeader,
        APIKeyQuery,
        APIKeyCookie,
        HTTPBasicCredentials,
        HTTPAuthorizationCredentials,
        SecurityScopes,
    )

    # Just verify they're importable and are actual classes
    assert callable(OAuth2PasswordBearer)
    assert callable(HTTPBearer)
    assert callable(HTTPBasic)
    assert callable(APIKeyHeader)
    assert callable(APIKeyQuery)
    assert callable(APIKeyCookie)
    assert HTTPBasicCredentials(username="a", password="b").username == "a"
    assert HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok").credentials == "tok"
    assert SecurityScopes(["read", "write"]).scope_str == "read write"


def test_fastapi_encoders_module():
    from fastapi.encoders import jsonable_encoder
    from fastapi.encoders import jsonable_encoder as J

    assert jsonable_encoder is J


def test_fastapi_status_module():
    from fastapi import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422


def test_fastapi_testclient_module():
    from fastapi.testclient import TestClient
    from fastapi.testclient import TestClient as JTC

    assert TestClient is JTC


def test_fastapi_middleware_cors():
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.cors import CORSMiddleware as JC

    assert CORSMiddleware is JC


# ── starlette imports ──────────────────────────────────────────────


def test_starlette_response_import():
    from starlette.responses import JSONResponse
    from fastapi.responses import JSONResponse as TurboJSONResponse

    assert JSONResponse is TurboJSONResponse


def test_starlette_request_import():
    from starlette.requests import Request
    from starlette.requests import Request as TurboRequest

    assert Request is TurboRequest


def test_starlette_status_import():
    from starlette import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500


def test_starlette_websocket_import():
    from starlette.websockets import WebSocket
    from starlette.websockets import WebSocket as TurboWS

    assert WebSocket is TurboWS


def test_starlette_exceptions_import():
    from starlette.exceptions import HTTPException
    from fastapi.exceptions import HTTPException as TurboHTTPException

    assert HTTPException is TurboHTTPException


def test_starlette_datastructures_import():
    from starlette.datastructures import URL, Headers, QueryParams, State

    assert URL is not None
    assert Headers is not None
    assert QueryParams is not None
    assert State is not None


def test_starlette_middleware_cors():
    from starlette.middleware.cors import CORSMiddleware
    from starlette.middleware.cors import CORSMiddleware as JC

    assert CORSMiddleware is JC


def test_starlette_concurrency_import():
    from starlette.concurrency import run_in_threadpool
    from fastapi.concurrency import run_in_threadpool as JR

    assert run_in_threadpool is JR


def test_starlette_background_import():
    from starlette.background import BackgroundTasks, BackgroundTask
    from starlette.background import BackgroundTasks as JBT, BackgroundTask as JBT1

    assert BackgroundTasks is JBT
    assert BackgroundTask is JBT1


# ── Shim install / uninstall ──────────────────────────────────────


def test_shim_uninstall_reinstall():
    """Uninstalling and reinstalling shims works."""
    import sys
    from fastapi_turbo.compat import uninstall, install

    # Shims should be installed
    assert "fastapi" in sys.modules

    uninstall()
    assert "fastapi" not in sys.modules

    install()
    assert "fastapi" in sys.modules

    # Verify imports still work
    from fastapi import FastAPI
    from fastapi import FastAPI as JF
    assert FastAPI is JF
