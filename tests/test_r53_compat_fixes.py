from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated

import pytest

import fastapi_turbo  # noqa: F401  # install compat shims
from fastapi import FastAPI, File, Query, UploadFile
from fastapi._compat import is_uploadfile_sequence_annotation
from fastapi._compat.shared import is_bytes_sequence_annotation
from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html
from fastapi.testclient import TestClient
from fastapi_turbo.http import Client as TurboClient, ConnectError
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.endpoints import HTTPEndpoint, WebSocketEndpoint
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute


def _assert_get_text(app, path: str, expected: str) -> None:
    for in_process in (True, False):
        with TestClient(app, in_process=in_process) as client:
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.text == expected


def test_add_route_and_route_decorator_use_starlette_request_endpoint():
    async def plain(request):
        return PlainTextResponse(f"plain:{request.url.path}")

    app = FastAPI()
    app.add_route("/plain", plain, methods=["GET"])

    @app.route("/decor", methods=["GET"])
    async def decorated(request):
        return PlainTextResponse(f"decor:{request.url.path}")

    _assert_get_text(app, "/plain", "plain:/plain")
    _assert_get_text(app, "/decor", "decor:/decor")


def test_add_route_supports_starlette_http_endpoint_classes():
    class Home(HTTPEndpoint):
        async def get(self, request):
            return PlainTextResponse(f"home:{request.url.path}")

    app = FastAPI()
    app.add_route("/home", Home)
    _assert_get_text(app, "/home", "home:/home")


def test_routes_kwarg_mount_serves_subapp_in_process_and_server():
    sub = FastAPI()

    @sub.get("/hi")
    def hi():
        return PlainTextResponse("mounted")

    app = FastAPI(routes=[Mount("/sub", app=sub)])
    _assert_get_text(app, "/sub/hi", "mounted")


def test_routes_kwarg_mount_with_routes_list_serves_http_and_websocket():
    async def hi(request):
        return PlainTextResponse(f"nested:{request.url.path}")

    async def ws_endpoint(websocket):
        await websocket.accept()
        await websocket.send_text("ws-mounted")
        await websocket.close()

    app = FastAPI(routes=[
        Mount("/sub", routes=[
            Route("/hi", hi),
            WebSocketRoute("/ws", ws_endpoint),
        ])
    ])

    with TestClient(app, in_process=True) as client:
        response = client.get("/sub/hi")
        assert response.status_code == 200, response.text
        assert response.text == "nested:/hi"
        with client.websocket_connect("/sub/ws") as websocket:
            assert websocket.receive_text() == "ws-mounted"


def test_websocket_route_objects_and_class_endpoints_dispatch_in_process():
    async def route_ws(websocket):
        await websocket.accept()
        await websocket.send_text("route-ws")
        await websocket.close()

    class Echo(WebSocketEndpoint):
        encoding = "text"

        async def on_connect(self, websocket):
            await websocket.accept()

        async def on_receive(self, websocket, data):
            await websocket.send_text(f"echo:{data}")
            await websocket.close()

    app = FastAPI(routes=[WebSocketRoute("/route-ws", route_ws)])
    app.add_websocket_route("/class-ws", Echo)

    with TestClient(app, in_process=True) as client:
        with client.websocket_connect("/route-ws") as websocket:
            assert websocket.receive_text() == "route-ws"
        with client.websocket_connect("/class-ws") as websocket:
            websocket.send_text("hi")
            assert websocket.receive_text() == "echo:hi"


class _PositionalMiddleware:
    def __init__(self, app, marker: str):
        self.app = app
        self.marker = marker

    async def __call__(self, scope, receive, send):
        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-marker", self.marker.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


class _FiltersModel(BaseModel):
    tag: list[str] | None = None


def test_middleware_positional_args_work_from_constructor_and_add_middleware():
    init_app = FastAPI(middleware=[Middleware(_PositionalMiddleware, "init")])

    @init_app.get("/init")
    def init_route():
        return {"ok": True}

    added_app = FastAPI()
    added_app.add_middleware(_PositionalMiddleware, "added")

    @added_app.get("/added")
    def added_route():
        return {"ok": True}

    with TestClient(init_app, in_process=True) as client:
        assert client.get("/init").headers["x-marker"] == "init"
    with TestClient(added_app, in_process=True) as client:
        assert client.get("/added").headers["x-marker"] == "added"


def test_add_event_handler_runs_startup_and_shutdown():
    events: list[str] = []
    app = FastAPI()
    app.add_event_handler("startup", lambda: events.append("startup"))
    app.add_event_handler("shutdown", lambda: events.append("shutdown"))

    @app.get("/")
    def root():
        return {"events": list(events)}

    with TestClient(app, in_process=True) as client:
        assert client.get("/").json() == {"events": ["startup"]}
    assert events == ["startup", "shutdown"]


def test_query_parameter_model_pep604_optional_list_collects_repeated_values():
    app = FastAPI()

    @app.get("/items")
    def items(filters: Annotated[_FiltersModel, Query()]):
        return {"tag": filters.tag}

    with TestClient(app, in_process=True) as client:
        response = client.get("/items?tag=alpha&tag=beta")
        assert response.status_code == 200, response.text
        assert response.json() == {"tag": ["alpha", "beta"]}


def test_private_compat_sequence_helpers_accept_pep604_unions():
    assert is_bytes_sequence_annotation(list[str] | list[bytes])
    assert is_uploadfile_sequence_annotation(list[str] | list[UploadFile])


def test_query_rejects_pep604_optional_bare_dict_at_decoration_time():
    with pytest.raises(
        AssertionError,
        match="Query parameter 'q' must be one of the supported types",
    ):
        app = FastAPI()

        @app.get("/items")
        def items(q: dict | None = Query(default=None)):
            return q


def test_optional_uploadfile_without_file_marker_is_file_param():
    app = FastAPI()

    @app.post("/uploadfile/")
    async def create_upload_file(file: UploadFile | None = None):
        if not file:
            return {"message": "No upload file sent"}
        return {"filename": file.filename}

    @app.post("/bytes/")
    async def create_file(file: bytes | None = File(default=None)):
        if not file:
            return {"message": "No file sent"}
        return {"file_size": len(file)}

    for in_process in (True, False):
        with TestClient(app, in_process=in_process) as client:
            assert client.post("/uploadfile/").json() == {
                "message": "No upload file sent",
            }
            response = client.post(
                "/uploadfile/",
                files={"file": ("test.txt", b"<file content>")},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"filename": "test.txt"}

            schema = client.get("/openapi.json").json()
            upload_schema = schema["paths"]["/uploadfile/"]["post"]
            assert "requestBody" in upload_schema
            assert "parameters" not in upload_schema


def test_user_custom_oauth2_redirect_route_is_not_removed_by_server_start():
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

    for in_process in (True, False):
        with TestClient(app, in_process=in_process) as client:
            response = client.get("/docs/oauth2-redirect")
            assert response.status_code == 200, response.text
            assert "oauth2RedirectUrl" in response.text or "swaggerUIRedirectOauth2" in response.text


def test_host_accepts_starlette_host_keyword():
    sub = FastAPI()

    @sub.get("/")
    def sub_home():
        return {"host": "matched"}

    app = FastAPI()
    app.host(host="example.com", app=sub)

    with TestClient(app, in_process=True) as client:
        response = client.get("/", headers={"host": "example.com"})
        assert response.status_code == 200, response.text
        assert response.json() == {"host": "matched"}


def test_response_is_asgi_callable_and_runs_background_task():
    called: list[str] = []
    response = PlainTextResponse(
        "ok",
        background=BackgroundTask(lambda: called.append("done")),
    )
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    asyncio.run(response({"type": "http"}, None, send))
    assert messages[0]["type"] == "http.response.start"
    assert messages[1] == {"type": "http.response.body", "body": b"ok"}
    assert called == ["done"]


def test_http_client_fast_path_attaches_request_url_and_persists_cookies():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "session=abc; Path=/")
            self.end_headers()
            self.wfile.write(self.path.encode("utf-8"))

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        client = TurboClient()
        response = client.get(f"{base}/hello")
        assert response.status_code == 200
        assert response.text == "/hello"
        assert response.request is not None
        assert str(response.url) == f"{base}/hello"
        assert dict(client.cookies) == {"session": "abc"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_client_fast_path_maps_connection_refusal_to_connect_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    client = TurboClient(timeout=0.2)
    with pytest.raises(ConnectError) as exc_info:
        client.get(f"http://127.0.0.1:{port}/")
    assert exc_info.value.request is not None
