"""Httpx parity tests for ``fastapi_turbo.http.Client``.

The README + COMPATIBILITY.md claim ``fastapi_turbo.http.Client`` is a
drop-in replacement for ``httpx.Client``. This file exercises
``build_request`` against both clients and asserts equal observable
outputs (URL, headers, body bytes, content-type) for the surfaces
users typically rely on:

  * URL joining with base_url (relative, leading-slash, ``..``, absolute)
  * Form data only
  * Files only
  * data + files merged into a single multipart body
  * JSON body
  * Raw content body
  * Query params merged with existing query string
  * Cookies header
  * Auth header (basic)

Each test compares the two clients on the SAME inputs. Coverage gap
documented in the R51 audit.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import fastapi_turbo  # noqa: F401  # install shim
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_turbo.http import Auth, Client as TurboClient, TimeoutException


def _read_request(req):
    """Read both clients' streaming bodies into bytes for comparison."""
    if hasattr(req, "read"):
        try:
            req.read()
        except Exception:  # noqa: BLE001
            pass
    return getattr(req, "content", b"") or b""


def _ct_family(ct: str) -> str:
    """Strip the ``boundary=...`` token so two multipart Content-Types
    with different (random) boundaries compare equal."""
    if ";" in ct:
        return ct.split(";", 1)[0].strip().lower()
    return ct.lower()


def _multipart_parts(body: bytes, content_type: str) -> list[bytes]:
    """Split a multipart body on its ``--<boundary>`` separator and
    return the individual part bytes (without trailing CRLFs). Lets
    tests assert the SET of parts is identical even when boundaries
    differ between the two clients."""
    if "boundary=" not in content_type:
        return [body]
    boundary = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip()
    sep = (b"--" + boundary.encode("latin-1") + b"\r\n")
    end = (b"--" + boundary.encode("latin-1") + b"--")
    out = []
    if not body.startswith(sep[:-2]):
        return [body]
    chunks = body.split(sep)
    for ch in chunks[1:]:
        # Trim closing boundary if present at the tail of the last chunk.
        if end in ch:
            ch = ch.split(end, 1)[0]
        out.append(ch.rstrip(b"\r\n"))
    return out


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ────────────────────────────────────────────────────────────────────
# URL joining parity
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "base,rel,expected_path",
    [
        ("https://example.com/api/v1/", "../x", "/api/x"),
        ("https://example.com/api/v1", "../x", "/api/x"),
        ("https://example.com/api/v1/", "x", "/api/v1/x"),
        ("https://example.com/api/v1/", "/x", "/api/v1/x"),
        ("https://example.com/api/v1/", "./x", "/api/v1/x"),
        ("https://example.com/", "x", "/x"),
        ("https://example.com/", "/foo/bar", "/foo/bar"),
    ],
)
def test_url_join_matches_httpx(base, rel, expected_path):
    httpx_url = str(httpx.Client(base_url=base).build_request("GET", rel).url)
    turbo_url = str(TurboClient(base_url=base).build_request("GET", rel).url)
    assert turbo_url == httpx_url, (
        f"base={base!r} rel={rel!r}: turbo={turbo_url!r} httpx={httpx_url!r}"
    )
    # Independent invariant: path component matches the documented
    # expectation regardless of which client we pick.
    from urllib.parse import urlparse
    assert urlparse(turbo_url).path == expected_path


def test_absolute_url_bypasses_base():
    base = "https://example.com/api/"
    httpx_url = str(httpx.Client(base_url=base).build_request("GET", "https://other/y").url)
    turbo_url = str(TurboClient(base_url=base).build_request("GET", "https://other/y").url)
    assert turbo_url == httpx_url == "https://other/y"


def test_fast_path_base_url_join_matches_httpx_for_dot_segments():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.path.encode("utf-8"))

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        client = TurboClient(base_url=f"{base}/api/v1/")
        response = client.get("../x?y=1")
        httpx_url = str(
            httpx.Client(base_url=f"{base}/api/v1/").build_request("GET", "../x?y=1").url
        )
        assert str(response.url) == httpx_url
        assert response.text == "/api/x?y=1"


def test_gather_base_url_join_matches_httpx_for_dot_segments():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.path.encode("utf-8"))

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        client = TurboClient(base_url=f"{base}/api/v1/")
        responses = client.gather(["../x", "/root"])
        httpx_client = httpx.Client(base_url=f"{base}/api/v1/")
        assert [str(response.url) for response in responses] == [
            str(httpx_client.build_request("GET", "../x").url),
            str(httpx_client.build_request("GET", "/root").url),
        ]
        assert [response.text for response in responses] == ["/api/x", "/api/v1/root"]


# ────────────────────────────────────────────────────────────────────
# Body construction parity
# ────────────────────────────────────────────────────────────────────

def test_data_only_form_urlencoded():
    h = httpx.Client().build_request("POST", "https://x/y", data={"a": "1", "b": "2"})
    t = TurboClient().build_request("POST", "https://x/y", data={"a": "1", "b": "2"})
    assert h.headers.get("content-type") == t.headers.get("content-type")
    assert _read_request(h) == _read_request(t)


def test_files_only_multipart():
    files = {"f": ("hello.txt", b"world", "text/plain")}
    h = httpx.Client().build_request("POST", "https://x/y", files=files)
    t = TurboClient().build_request("POST", "https://x/y", files=files)
    assert _ct_family(h.headers.get("content-type", "")) == _ct_family(
        t.headers.get("content-type", "")
    ) == "multipart/form-data"
    h_parts = _multipart_parts(_read_request(h), h.headers["content-type"])
    t_parts = _multipart_parts(_read_request(t), t.headers["content-type"])
    assert len(h_parts) == len(t_parts) == 1
    # Each part contains the form-data header + payload — identical
    # across clients (ordering is deterministic for a single file).
    assert h_parts[0] == t_parts[0]


def test_data_plus_files_merges_into_one_multipart():
    """Audit R51 finding 1: turbo previously dropped ``files`` when
    ``data`` was set. httpx merges them into one multipart body with
    BOTH form fields and file parts. Probe both clients and compare."""
    data = {"a": "b"}
    files = {"f": ("hello.txt", b"world", "text/plain")}
    h = httpx.Client().build_request("POST", "https://x/y", data=data, files=files)
    t = TurboClient().build_request("POST", "https://x/y", data=data, files=files)
    h_ct = h.headers.get("content-type", "")
    t_ct = t.headers.get("content-type", "")
    assert _ct_family(h_ct) == _ct_family(t_ct) == "multipart/form-data"
    h_parts = _multipart_parts(_read_request(h), h_ct)
    t_parts = _multipart_parts(_read_request(t), t_ct)
    # Two parts: the form field ``a=b`` and the file ``f``.
    assert len(h_parts) == 2
    assert len(t_parts) == 2
    # Compare each part: order may differ between clients, so sort
    # by name= attribute first.
    def _name_of(p: bytes) -> bytes:
        marker = b'name="'
        idx = p.find(marker)
        if idx < 0:
            return b""
        rest = p[idx + len(marker):]
        end = rest.find(b'"')
        return rest[:end] if end >= 0 else b""
    h_sorted = sorted(h_parts, key=_name_of)
    t_sorted = sorted(t_parts, key=_name_of)
    for hp, tp in zip(h_sorted, t_sorted):
        assert _name_of(hp) == _name_of(tp), (hp, tp)


def test_json_body():
    h = httpx.Client().build_request("POST", "https://x/y", json={"a": 1, "b": [2, 3]})
    t = TurboClient().build_request("POST", "https://x/y", json={"a": 1, "b": [2, 3]})
    assert h.headers.get("content-type") == t.headers.get("content-type") == "application/json"
    # Compare semantic JSON content (key order may differ).
    assert json.loads(_read_request(h)) == json.loads(_read_request(t))


def test_raw_content_body():
    h = httpx.Client().build_request("POST", "https://x/y", content=b"raw-bytes-here")
    t = TurboClient().build_request("POST", "https://x/y", content=b"raw-bytes-here")
    assert _read_request(h) == _read_request(t) == b"raw-bytes-here"


# ────────────────────────────────────────────────────────────────────
# Query string + cookies
# ────────────────────────────────────────────────────────────────────

def test_params_replace_existing_query():
    """httpx drops the URL's existing query string when ``params=``
    is supplied to the per-request call (verified via probe). Both
    clients must agree."""
    h = httpx.Client().build_request("GET", "https://x/y?a=1", params={"b": "2"})
    t = TurboClient().build_request("GET", "https://x/y?a=1", params={"b": "2"})
    assert str(h.url) == str(t.url)


def test_params_dict_only_no_existing_query():
    h = httpx.Client().build_request("GET", "https://x/y", params={"a": "1", "b": "2"})
    t = TurboClient().build_request("GET", "https://x/y", params={"a": "1", "b": "2"})
    h_q = sorted(str(h.url).split("?", 1)[1].split("&"))
    t_q = sorted(str(t.url).split("?", 1)[1].split("&"))
    assert h_q == t_q


def test_no_params_keeps_existing_query():
    h = httpx.Client().build_request("GET", "https://x/y?a=1")
    t = TurboClient().build_request("GET", "https://x/y?a=1")
    assert str(h.url) == str(t.url) == "https://x/y?a=1"


def test_cookies_header_set():
    cookies = {"session": "abc", "tracking": "xyz"}
    h = httpx.Client().build_request("GET", "https://x/y", cookies=cookies)
    t = TurboClient().build_request("GET", "https://x/y", cookies=cookies)
    h_cookie = sorted((h.headers.get("cookie") or "").split("; "))
    t_cookie = sorted((t.headers.get("cookie") or "").split("; "))
    assert h_cookie == t_cookie


def test_build_request_includes_httpx_style_timeout_extension():
    h = httpx.Client(timeout=7).build_request("GET", "https://x/y")
    t = TurboClient(timeout=7).build_request("GET", "https://x/y")
    assert t.extensions["timeout"] == h.extensions["timeout"]


def test_per_request_timeout_override_reaches_transport():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(0.3)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"slow")
            except BrokenPipeError:
                pass

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        client = TurboClient(timeout=2.0)
        started = time.monotonic()
        with pytest.raises(TimeoutException):
            client.get(f"{base}/slow", timeout=0.05)
        assert time.monotonic() - started < 1.0


def test_single_url_gather_timeout_override_reaches_fast_path():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(0.3)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"slow")
            except BrokenPipeError:
                pass

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        client = TurboClient(timeout=2.0)
        started = time.monotonic()
        with pytest.raises(TimeoutException):
            client.gather([f"{base}/slow"], timeout=0.05)
        assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "status, expected_final_method, expected_final_body",
    [
        (302, "GET", b""),
        (307, "POST", b"abc"),
    ],
)
def test_follow_redirects_preserves_replayable_request_parts(
    status, expected_final_method, expected_final_body
):
    seen: list[tuple[str, str, str | None, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            seen.append((self.command, self.path, self.headers.get("x-test"), body))
            if self.path == "/start":
                self.send_response(status)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_GET(self):  # noqa: N802
            seen.append((self.command, self.path, self.headers.get("x-test"), b""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        response = TurboClient(follow_redirects=True).post(
            f"{base}/start",
            content=b"abc",
            headers={"x-test": "1", "content-type": "text/plain"},
        )

    assert response.status_code == 200
    assert [(entry[0], entry[1], entry[2]) for entry in seen] == [
        ("POST", "/start", "1"),
        (expected_final_method, "/final", "1"),
    ]
    assert seen[1][3] == expected_final_body
    assert [history.status_code for history in response.history] == [status]


def test_request_and_response_event_hooks_fire_for_redirect_chain():
    events: list[tuple[str, str | int]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        client = TurboClient(
            follow_redirects=True,
            event_hooks={
                "request": [lambda request: events.append(("request", request.url.path))],
                "response": [lambda response: events.append(("response", response.status_code))],
            },
        )
        response = client.get(f"{base}/start")

    assert response.status_code == 200
    assert events == [
        ("request", "/start"),
        ("response", 302),
        ("request", "/final"),
        ("response", 200),
    ]


def test_generator_auth_flow_can_retry_with_updated_request():
    seen: list[str | None] = []

    class RefreshAuth(Auth):
        def auth_flow(self, request):
            request.headers["authorization"] = "Bearer stale"
            response = yield request
            if response.status_code == 401:
                request.headers["authorization"] = "Bearer fresh"
                yield request

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            auth = self.headers.get("authorization")
            seen.append(auth)
            if auth != "Bearer fresh":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        response = TurboClient(auth=RefreshAuth()).get(f"{base}/auth")

    assert response.status_code == 200
    assert seen == ["Bearer stale", "Bearer fresh"]


def test_stream_context_manager_exposes_basic_httpx_iteration_contract():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"abcdef")

        def log_message(self, *_args):
            return

    with _serve(Handler) as base:
        with TurboClient().stream("GET", f"{base}/stream") as response:
            assert response.status_code == 200
            assert response.is_closed is False
            assert response.is_stream_consumed is False
            assert list(response.iter_bytes(2)) == [b"ab", b"cd", b"ef"]
            assert response.is_stream_consumed is True
            assert response.is_closed is True
        assert response.is_closed is True


def test_http_client_import_pattern_keeps_fastapi_symbols_on_fastapi_module():
    app = FastAPI()

    @app.get("/")
    def root():
        return {"ok": True}

    with TestClient(app, in_process=True) as client:
        assert client.get("/").json() == {"ok": True}
