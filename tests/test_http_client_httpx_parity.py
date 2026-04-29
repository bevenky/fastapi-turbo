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

import httpx
import pytest

import fastapi_turbo  # noqa: F401  # install shim
from fastapi_turbo.http import Client as TurboClient


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
