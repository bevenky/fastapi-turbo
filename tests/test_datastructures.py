"""Phase 9-10 tests: datastructures, Request, encoders, status, background, etc."""

import asyncio


# ── Datastructures ─────────────────────────────────────────────────


def test_url_from_string():
    from fastapi_rs.datastructures import URL

    u = URL("https://example.com:8443/path?q=1#frag")
    assert u.scheme == "https"
    assert u.hostname == "example.com"
    assert u.port == 8443
    assert u.path == "/path"
    assert u.query == "q=1"
    assert u.fragment == "frag"
    assert str(u) == "https://example.com:8443/path?q=1#frag"


def test_url_from_scope():
    from fastapi_rs.datastructures import URL

    scope = {
        "scheme": "http",
        "server": ("localhost", 8000),
        "path": "/api/items",
        "query_string": "page=2",
    }
    u = URL(scope)
    assert u.scheme == "http"
    assert u.hostname == "localhost"
    assert u.path == "/api/items"
    assert "page=2" in str(u)


def test_headers_case_insensitive():
    from fastapi_rs.datastructures import Headers

    h = Headers({"Content-Type": "application/json", "X-Token": "abc"})
    assert h["content-type"] == "application/json"
    assert h["CONTENT-TYPE"] == "application/json"
    assert h.get("x-token") == "abc"
    assert h.get("missing", "default") == "default"
    assert "content-type" in h
    assert len(h) == 2


def test_headers_from_tuples():
    from fastapi_rs.datastructures import Headers

    h = Headers([(b"content-type", b"text/html"), (b"x-custom", b"val")])
    assert h["content-type"] == "text/html"
    assert h["x-custom"] == "val"


def test_query_params():
    from fastapi_rs.datastructures import QueryParams

    qp = QueryParams("q=python&limit=10&q=rust")
    assert qp["q"] == "python"  # first value
    assert qp.getlist("q") == ["python", "rust"]
    assert qp["limit"] == "10"
    assert "q" in qp
    assert "missing" not in qp


def test_address():
    from fastapi_rs.datastructures import Address

    a = Address(("127.0.0.1", 8080))
    assert a.host == "127.0.0.1"
    assert a.port == 8080


def test_state():
    from fastapi_rs.datastructures import State

    s = State()
    s.counter = 0
    s.counter += 1
    assert s.counter == 1

    s2 = State({"a": 1, "b": 2})
    assert s2.a == 1
    assert s2.b == 2


# ── Request ────────────────────────────────────────────────────────


def test_request_basic():
    from fastapi_rs.requests import Request

    scope = {
        "method": "POST",
        "path": "/items",
        "query_string": "page=1",
        "headers": {"content-type": "application/json"},
        "path_params": {"item_id": "42"},
    }
    req = Request(scope)
    assert req.method == "POST"
    assert req.headers["content-type"] == "application/json"
    assert req.query_params["page"] == "1"
    assert req.path_params == {"item_id": "42"}


def test_request_cookies():
    from fastapi_rs.requests import Request

    scope = {
        "headers": {"cookie": "session=abc123; theme=dark"},
    }
    req = Request(scope)
    assert req.cookies["session"] == "abc123"
    assert req.cookies["theme"] == "dark"


def test_request_body():
    from fastapi_rs.requests import Request

    scope = {"_body": b'{"key": "value"}'}
    req = Request(scope)
    body = asyncio.run(req.body())
    assert body == b'{"key": "value"}'


def test_request_json():
    from fastapi_rs.requests import Request

    scope = {"_body": b'{"key": "value"}'}
    req = Request(scope)
    data = asyncio.run(req.json())
    assert data == {"key": "value"}


def test_request_state():
    from fastapi_rs.requests import Request

    req = Request()
    req.state.user = "alice"
    assert req.state.user == "alice"


def test_request_client():
    from fastapi_rs.requests import Request

    scope = {"client": ("192.168.1.1", 54321)}
    req = Request(scope)
    assert req.client.host == "192.168.1.1"
    assert req.client.port == 54321


# ── jsonable_encoder ───────────────────────────────────────────────


def test_jsonable_encoder_primitives():
    from fastapi_rs.encoders import jsonable_encoder

    assert jsonable_encoder("hello") == "hello"
    assert jsonable_encoder(42) == 42
    assert jsonable_encoder(3.14) == 3.14
    assert jsonable_encoder(True) is True
    assert jsonable_encoder(None) is None


def test_jsonable_encoder_dict():
    from fastapi_rs.encoders import jsonable_encoder

    result = jsonable_encoder({"a": 1, "b": [2, 3]})
    assert result == {"a": 1, "b": [2, 3]}


def test_jsonable_encoder_list():
    from fastapi_rs.encoders import jsonable_encoder

    result = jsonable_encoder([1, "two", 3.0])
    assert result == [1, "two", 3.0]


def test_jsonable_encoder_pydantic():
    from pydantic import BaseModel
    from fastapi_rs.encoders import jsonable_encoder

    class Item(BaseModel):
        name: str
        price: float

    item = Item(name="widget", price=9.99)
    result = jsonable_encoder(item)
    assert result == {"name": "widget", "price": 9.99}


def test_jsonable_encoder_datetime():
    import datetime
    from fastapi_rs.encoders import jsonable_encoder

    dt = datetime.datetime(2024, 1, 15, 12, 30, 0)
    result = jsonable_encoder(dt)
    assert "2024-01-15" in result
    assert "12:30" in result


def test_jsonable_encoder_uuid():
    import uuid
    from fastapi_rs.encoders import jsonable_encoder

    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    result = jsonable_encoder(u)
    assert result == "12345678-1234-5678-1234-567812345678"


def test_jsonable_encoder_enum():
    import enum
    from fastapi_rs.encoders import jsonable_encoder

    class Color(enum.Enum):
        RED = "red"
        BLUE = "blue"

    assert jsonable_encoder(Color.RED) == "red"


def test_jsonable_encoder_exclude_none():
    from fastapi_rs.encoders import jsonable_encoder

    data = {"a": 1, "b": None, "c": 3}
    result = jsonable_encoder(data, exclude_none=True)
    assert result == {"a": 1, "c": 3}


# ── Status codes ───────────────────────────────────────────────────


def test_status_codes():
    from fastapi_rs import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_201_CREATED == 201
    assert status.HTTP_204_NO_CONTENT == 204
    assert status.HTTP_301_MOVED_PERMANENTLY == 301
    assert status.HTTP_302_FOUND == 302
    assert status.HTTP_304_NOT_MODIFIED == 304
    assert status.HTTP_307_TEMPORARY_REDIRECT == 307
    assert status.HTTP_400_BAD_REQUEST == 400
    assert status.HTTP_401_UNAUTHORIZED == 401
    assert status.HTTP_403_FORBIDDEN == 403
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_405_METHOD_NOT_ALLOWED == 405
    assert status.HTTP_409_CONFLICT == 409
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422
    assert status.HTTP_429_TOO_MANY_REQUESTS == 429
    assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500
    assert status.HTTP_502_BAD_GATEWAY == 502
    assert status.HTTP_503_SERVICE_UNAVAILABLE == 503


def test_ws_status_codes():
    from fastapi_rs import status

    assert status.WS_1000_NORMAL_CLOSURE == 1000
    assert status.WS_1001_GOING_AWAY == 1001
    assert status.WS_1008_POLICY_VIOLATION == 1008


# ── BackgroundTasks ────────────────────────────────────────────────


def test_background_tasks():
    from fastapi_rs.background import BackgroundTasks

    results = []

    def sync_task(value):
        results.append(value)

    async def async_task(value):
        results.append(value)

    bt = BackgroundTasks()
    bt.add_task(sync_task, "sync")
    bt.add_task(async_task, "async")

    asyncio.run(bt._run())
    assert results == ["sync", "async"]


# ── Security classes ───────────────────────────────────────────────


def test_oauth2_password_bearer():
    from fastapi_rs.security import OAuth2PasswordBearer

    scheme = OAuth2PasswordBearer(tokenUrl="/token")
    assert scheme.tokenUrl == "/token"
    assert scheme.scheme_name == "OAuth2PasswordBearer"
    assert scheme.model["type"] == "oauth2"

    # Test __call__ with Request (new FastAPI-compatible signature)
    from fastapi_rs.requests import Request
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer mytoken123")]})
    token = asyncio.run(scheme(req))
    assert token == "mytoken123"


def test_oauth2_password_bearer_no_token():
    import pytest
    from fastapi_rs.security import OAuth2PasswordBearer
    from fastapi_rs.exceptions import HTTPException
    from fastapi_rs.requests import Request

    scheme = OAuth2PasswordBearer(tokenUrl="/token")
    req = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scheme(req))
    assert exc_info.value.status_code == 401


def test_oauth2_password_bearer_no_auto_error():
    from fastapi_rs.security import OAuth2PasswordBearer
    from fastapi_rs.requests import Request

    scheme = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)
    req = Request({"type": "http", "headers": []})
    result = asyncio.run(scheme(req))
    assert result is None


def test_http_bearer():
    from fastapi_rs.security import HTTPBearer
    from fastapi_rs.requests import Request

    scheme = HTTPBearer()
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer xyz")]})
    cred = asyncio.run(scheme(req))
    assert cred.scheme == "Bearer"
    assert cred.credentials == "xyz"


def test_http_basic():
    import base64
    from fastapi_rs.security import HTTPBasic
    from fastapi_rs.requests import Request

    scheme = HTTPBasic()
    encoded = base64.b64encode(b"user:pass").decode()
    req = Request({"type": "http", "headers": [(b"authorization", f"Basic {encoded}".encode())]})
    cred = asyncio.run(scheme(req))
    assert cred.username == "user"
    assert cred.password == "pass"


# ── Concurrency ────────────────────────────────────────────────────


def test_run_in_threadpool():
    from fastapi_rs.concurrency import run_in_threadpool

    def sync_fn(x, y):
        return x + y

    result = asyncio.run(run_in_threadpool(sync_fn, 3, 4))
    assert result == 7


def test_run_in_threadpool_with_kwargs():
    from fastapi_rs.concurrency import run_in_threadpool

    def sync_fn(x, y=10):
        return x + y

    result = asyncio.run(run_in_threadpool(sync_fn, 3, y=20))
    assert result == 23
