"""Phase 9-10 tests: compatibility shims for fastapi.* and starlette.* imports."""

import fastapi_rs  # noqa: F401 — ensure shims are installed


# ── fastapi top-level imports ──────────────────────────────────────


def test_fastapi_import_fastapi():
    """from fastapi import FastAPI resolves to fastapi_rs."""
    from fastapi import FastAPI
    from fastapi_rs import FastAPI as JamunFastAPI

    assert FastAPI is JamunFastAPI


def test_fastapi_depends_import():
    from fastapi import Depends
    from fastapi_rs import Depends as JamunDepends

    assert Depends is JamunDepends


def test_fastapi_http_exception_import():
    from fastapi import HTTPException
    from fastapi_rs import HTTPException as JamunHTTPException

    assert HTTPException is JamunHTTPException


def test_fastapi_query_path_import():
    from fastapi import Query, Path, Header, Cookie, Body, Form, File
    from fastapi_rs import Query as JQ, Path as JP, Header as JH
    from fastapi_rs import Cookie as JC, Body as JB, Form as JFo, File as JFi

    assert Query is JQ
    assert Path is JP
    assert Header is JH
    assert Cookie is JC
    assert Body is JB
    assert Form is JFo
    assert File is JFi


def test_fastapi_response_import():
    from fastapi import JSONResponse, HTMLResponse, Response
    from fastapi_rs import JSONResponse as JJ, HTMLResponse as JH, Response as JR

    assert JSONResponse is JJ
    assert HTMLResponse is JH
    assert Response is JR


def test_fastapi_request_import():
    from fastapi import Request
    from fastapi_rs import Request as JamunRequest

    assert Request is JamunRequest


def test_fastapi_uploadfile_import():
    from fastapi import UploadFile
    from fastapi_rs import UploadFile as JamunUploadFile

    assert UploadFile is JamunUploadFile


def test_fastapi_apirouter_import():
    from fastapi import APIRouter
    from fastapi_rs import APIRouter as JamunAPIRouter

    assert APIRouter is JamunAPIRouter


def test_fastapi_background_tasks_import():
    from fastapi import BackgroundTasks
    from fastapi_rs import BackgroundTasks as JamunBT

    assert BackgroundTasks is JamunBT


def test_fastapi_websocket_import():
    from fastapi import WebSocket
    from fastapi_rs import WebSocket as JamunWS

    assert WebSocket is JamunWS


# ── fastapi submodule imports ──────────────────────────────────────


def test_fastapi_responses_module():
    from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
    from fastapi_rs.responses import JSONResponse as JJ
    from fastapi_rs.responses import HTMLResponse as JH
    from fastapi_rs.responses import PlainTextResponse as JP

    assert JSONResponse is JJ
    assert HTMLResponse is JH
    assert PlainTextResponse is JP


def test_fastapi_routing_module():
    from fastapi.routing import APIRouter, APIRoute
    from fastapi_rs.routing import APIRouter as JR, APIRoute as JA

    assert APIRouter is JR
    assert APIRoute is JA


def test_fastapi_exceptions_module():
    from fastapi.exceptions import HTTPException, RequestValidationError
    from fastapi_rs.exceptions import HTTPException as JH, RequestValidationError as JR

    assert HTTPException is JH
    assert RequestValidationError is JR


def test_fastapi_security_import():
    from fastapi.security import OAuth2PasswordBearer
    from fastapi_rs.security import OAuth2PasswordBearer as JamunOAuth2

    assert OAuth2PasswordBearer is JamunOAuth2


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
    assert HTTPBasicCredentials("a", "b").username == "a"
    assert HTTPAuthorizationCredentials("Bearer", "tok").credentials == "tok"
    assert SecurityScopes(["read", "write"]).scope_str == "read write"


def test_fastapi_encoders_module():
    from fastapi.encoders import jsonable_encoder
    from fastapi_rs.encoders import jsonable_encoder as J

    assert jsonable_encoder is J


def test_fastapi_status_module():
    from fastapi import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422


def test_fastapi_testclient_module():
    from fastapi.testclient import TestClient
    from fastapi_rs.testclient import TestClient as JTC

    assert TestClient is JTC


def test_fastapi_middleware_cors():
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi_rs.middleware.cors import CORSMiddleware as JC

    assert CORSMiddleware is JC


# ── starlette imports ──────────────────────────────────────────────


def test_starlette_response_import():
    from starlette.responses import JSONResponse
    from fastapi_rs.responses import JSONResponse as JamunJSONResponse

    assert JSONResponse is JamunJSONResponse


def test_starlette_request_import():
    from starlette.requests import Request
    from fastapi_rs.requests import Request as JamunRequest

    assert Request is JamunRequest


def test_starlette_status_import():
    from starlette import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500


def test_starlette_websocket_import():
    from starlette.websockets import WebSocket
    from fastapi_rs.websockets import WebSocket as JamunWS

    assert WebSocket is JamunWS


def test_starlette_exceptions_import():
    from starlette.exceptions import HTTPException
    from fastapi_rs.exceptions import HTTPException as JamunHTTPException

    assert HTTPException is JamunHTTPException


def test_starlette_datastructures_import():
    from starlette.datastructures import URL, Headers, QueryParams, State

    assert URL is not None
    assert Headers is not None
    assert QueryParams is not None
    assert State is not None


def test_starlette_middleware_cors():
    from starlette.middleware.cors import CORSMiddleware
    from fastapi_rs.middleware.cors import CORSMiddleware as JC

    assert CORSMiddleware is JC


def test_starlette_concurrency_import():
    from starlette.concurrency import run_in_threadpool
    from fastapi_rs.concurrency import run_in_threadpool as JR

    assert run_in_threadpool is JR


def test_starlette_background_import():
    from starlette.background import BackgroundTasks, BackgroundTask
    from fastapi_rs.background import BackgroundTasks as JBT, BackgroundTask as JBT1

    assert BackgroundTasks is JBT
    assert BackgroundTask is JBT1


# ── Shim install / uninstall ──────────────────────────────────────


def test_shim_uninstall_reinstall():
    """Uninstalling and reinstalling shims works."""
    import sys
    from fastapi_rs.compat import uninstall, install

    # Shims should be installed
    assert "fastapi" in sys.modules

    uninstall()
    assert "fastapi" not in sys.modules

    install()
    assert "fastapi" in sys.modules

    # Verify imports still work
    from fastapi import FastAPI
    from fastapi_rs import FastAPI as JF
    assert FastAPI is JF
