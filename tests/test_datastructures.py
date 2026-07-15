"""Datastructure / door-request / concurrency pins the upstream suite lacks.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * jsonable_encoder primitives/dict/list/pydantic/datetime/enum
                                → tests/test_jsonable_encoder.py (25 tests)
  * jsonable_encoder UUID       → tests/test_tutorial/test_extra_data_types/
                                  test_tutorial001.py (UUID round-trip)
  * jsonable_encoder exclude_none
                                → tests/test_serialize_response_model.py
                                  (response_model_exclude_none end-to-end)
  * HTTP + WS status constants  → upstream usage pins (tests/test_ws_router.py
                                  asserts WS_1000/WS_1008; HTTP_* asserted
                                  suite-wide) — real starlette module
  * BackgroundTasks sync+async execution
                                → tests/test_tutorial/test_background_tasks/
                                  test_tutorial001.py + test_tutorial002.py
  * run_in_threadpool positional-only call
                                → retained test_run_in_threadpool_with_kwargs
                                  below (superset) + upstream
                                  tests/test_dependency_wrapped.py

KEPT: real-Starlette datastructure shape pins (the recent re-point aimed
these at the REAL classes — they guard the shim mapping and have no
upstream-FastAPI twin), the ``_door_make_request`` scope-shape unit pins
(door internals, no upstream analogue), and the unique-line carrier
``test_run_in_threadpool_with_kwargs`` (sole cover of concurrency.py:30).

KEPT AS LOCAL-COVERAGE CARRIERS (twins exist upstream —
tests/test_tutorial/test_security/test_tutorial001.py,
tests/test_security_oauth2_password_bearer_optional.py,
tests/test_security_http_bearer.py, tests/test_security_http_basic_realm.py —
but the local fast suite would lose the security.py ``__call__`` arcs
entirely; the ≤0.2% local-coverage gate keeps them here): the five
direct-call security scheme tests below.
"""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import asyncio


# ── Datastructures ─────────────────────────────────────────────────


def test_url_from_string():
    from starlette.datastructures import URL

    u = URL("https://example.com:8443/path?q=1#frag")
    assert u.scheme == "https"
    assert u.hostname == "example.com"
    assert u.port == 8443
    assert u.path == "/path"
    assert u.query == "q=1"
    assert u.fragment == "frag"
    assert str(u) == "https://example.com:8443/path?q=1#frag"


def test_url_from_scope():
    from starlette.datastructures import URL

    scope = {
        "scheme": "http",
        "server": ("localhost", 8000),
        "path": "/api/items",
        "query_string": b"page=2",
        "headers": [],
    }
    u = URL(scope=scope)  # real Starlette URL takes scope as a keyword
    assert u.scheme == "http"
    assert u.hostname == "localhost"
    assert u.path == "/api/items"
    assert "page=2" in str(u)


def test_headers_case_insensitive():
    from starlette.datastructures import Headers

    h = Headers({"Content-Type": "application/json", "X-Token": "abc"})
    assert h["content-type"] == "application/json"
    assert h["CONTENT-TYPE"] == "application/json"
    assert h.get("x-token") == "abc"
    assert h.get("missing", "default") == "default"
    assert "content-type" in h
    assert len(h) == 2


def test_headers_from_tuples():
    from starlette.datastructures import Headers

    h = Headers(raw=[(b"content-type", b"text/html"), (b"x-custom", b"val")])
    assert h["content-type"] == "text/html"
    assert h["x-custom"] == "val"


def test_query_params():
    from starlette.datastructures import QueryParams

    qp = QueryParams("q=python&limit=10&q=rust")
    assert qp["q"] == "rust"  # Starlette semantics: last wins
    assert qp.getlist("q") == ["python", "rust"]
    assert qp["limit"] == "10"
    assert "q" in qp
    assert "missing" not in qp


def test_address():
    from starlette.datastructures import Address

    a = Address("127.0.0.1", 8080)  # real Starlette Address(host, port) NamedTuple
    assert a.host == "127.0.0.1"
    assert a.port == 8080


def test_state():
    from starlette.datastructures import State

    s = State()
    s.counter = 0
    s.counter += 1
    assert s.counter == 1

    s2 = State({"a": 1, "b": 2})
    assert s2.a == 1
    assert s2.b == 2


# ── Request ────────────────────────────────────────────────────────


def test_request_basic():
    # The door builds requests via _door_make_request (real Starlette Request,
    # real scope shapes: list[(bytes,bytes)] headers, bytes query_string).
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({
        "type": "http",
        "method": "POST",
        "path": "/items",
        "query_string": b"page=1",
        "headers": [(b"content-type", b"application/json")],
        "path_params": {"item_id": "42"},
    })
    assert req.method == "POST"
    assert req.headers["content-type"] == "application/json"
    assert req.query_params["page"] == "1"
    assert req.path_params == {"item_id": "42"}


def test_request_cookies():
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({"headers": [(b"cookie", b"session=abc123; theme=dark")]})
    assert req.cookies["session"] == "abc123"
    assert req.cookies["theme"] == "dark"


def test_request_body():
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({"_body": b'{"key": "value"}'})
    body = asyncio.run(req.body())
    assert body == b'{"key": "value"}'


def test_request_json():
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({"_body": b'{"key": "value"}'})
    data = asyncio.run(req.json())
    assert data == {"key": "value"}


def test_request_state():
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({})
    req.state.user = "alice"
    assert req.state.user == "alice"


def test_request_client():
    from fastapi_turbo.requests import _door_make_request

    req = _door_make_request({"client": ("192.168.1.1", 54321)})
    assert req.client.host == "192.168.1.1"
    assert req.client.port == 54321


# ── Security classes (local-coverage carriers, see header) ────────


def test_oauth2_password_bearer():
    from fastapi.security import OAuth2PasswordBearer

    scheme = OAuth2PasswordBearer(tokenUrl="/token")
    # Real fastapi.security scheme: ``.model`` is the pydantic SecurityScheme
    # model (the clone's dict-shaped ``.model`` is retired).
    assert scheme.model.flows.password.tokenUrl == "/token"
    assert scheme.scheme_name == "OAuth2PasswordBearer"
    assert scheme.model.type_.value == "oauth2"

    # Test __call__ with Request (new FastAPI-compatible signature)
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer mytoken123")]})
    token = asyncio.run(scheme(req))
    assert token == "mytoken123"


def test_oauth2_password_bearer_no_token():
    import pytest
    from fastapi.security import OAuth2PasswordBearer
    from fastapi.exceptions import HTTPException
    from starlette.requests import Request

    scheme = OAuth2PasswordBearer(tokenUrl="/token")
    req = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scheme(req))
    assert exc_info.value.status_code == 401


def test_oauth2_password_bearer_no_auto_error():
    from fastapi.security import OAuth2PasswordBearer
    from starlette.requests import Request

    scheme = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)
    req = Request({"type": "http", "headers": []})
    result = asyncio.run(scheme(req))
    assert result is None


def test_http_bearer():
    from fastapi.security import HTTPBearer
    from starlette.requests import Request

    scheme = HTTPBearer()
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer xyz")]})
    cred = asyncio.run(scheme(req))
    assert cred.scheme == "Bearer"
    assert cred.credentials == "xyz"


def test_http_basic():
    import base64
    from fastapi.security import HTTPBasic
    from starlette.requests import Request

    scheme = HTTPBasic()
    encoded = base64.b64encode(b"user:pass").decode()
    req = Request({"type": "http", "headers": [(b"authorization", f"Basic {encoded}".encode())]})
    cred = asyncio.run(scheme(req))
    assert cred.username == "user"
    assert cred.password == "pass"


# ── Concurrency ────────────────────────────────────────────────────


def test_run_in_threadpool_with_kwargs():
    from fastapi.concurrency import run_in_threadpool

    def sync_fn(x, y=10):
        return x + y

    result = asyncio.run(run_in_threadpool(sync_fn, 3, y=20))
    assert result == 23
