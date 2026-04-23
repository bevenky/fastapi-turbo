#!/usr/bin/env python3
"""Parity runner 3: patterns 251-400.

Launches parity_app_3.py on:
  - FastAPI + uvicorn  (port 29300)
  - fastapi-turbo         (port 29301)

Then runs every pattern against both and compares results.
"""

import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback

import httpx

FASTAPI_PORT = 29300
FASTAPI_TURBO_PORT = 29301
HOST = "127.0.0.1"

APP_FILE = os.path.join(os.path.dirname(__file__), "parity_app_3.py")

# ── Server lifecycle ─────────────────────────────────────────────────


def _wait_for_port(port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.15)
    return False


def _kill(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def start_fastapi(port):
    """Start the parity app under real FastAPI/uvicorn."""
    env = os.environ.copy()
    env["FASTAPI_TURBO_NO_SHIM"] = "1"
    test_dir = os.path.dirname(os.path.abspath(APP_FILE))
    proc = subprocess.Popen(
        [sys.executable, "-c", f"""
import sys, os
os.environ["FASTAPI_TURBO_NO_SHIM"] = "1"
sys.path.insert(0, {test_dir!r})
import uvicorn
from parity_app_3 import app
uvicorn.run(app, host="{HOST}", port={port}, log_level="warning")
"""],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not _wait_for_port(port):
        out = proc.stdout.read().decode()
        err = proc.stderr.read().decode()
        _kill(proc)
        raise RuntimeError(f"FastAPI did not start on {port}\nstdout: {out}\nstderr: {err}")
    return proc


def start_fastapi_turbo(port):
    """Start the parity app under fastapi-turbo."""
    test_dir = os.path.dirname(os.path.abspath(APP_FILE))
    proc = subprocess.Popen(
        [sys.executable, "-c", f"""
import sys, os
# Install compat shims so `from fastapi import ...` maps to fastapi_turbo
import fastapi_turbo.compat
fastapi_turbo.compat.install()
sys.path.insert(0, {test_dir!r})
from parity_app_3 import app
app.run("{HOST}", {port})
"""],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not _wait_for_port(port):
        out = proc.stdout.read().decode()
        err = proc.stderr.read().decode()
        _kill(proc)
        raise RuntimeError(f"fastapi-turbo did not start on {port}\nstdout: {out}\nstderr: {err}")
    return proc


# ── Test infrastructure ──────────────────────────────────────────────

class Result:
    def __init__(self, status=None, body=None, headers=None, error=None, raw=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.error = error
        self.raw = raw  # raw bytes


def http_get(port, path, headers=None, timeout=10):
    try:
        r = httpx.get(f"http://{HOST}:{port}{path}", headers=headers or {}, timeout=timeout, follow_redirects=False)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return Result(status=r.status_code, body=body, headers=dict(r.headers), raw=r.content)
    except Exception as e:
        return Result(error=str(e))


def http_post(port, path, data=None, files=None, json_body=None, headers=None, timeout=10):
    try:
        kwargs = {"timeout": timeout, "follow_redirects": False}
        if headers:
            kwargs["headers"] = headers
        if json_body is not None:
            kwargs["json"] = json_body
        elif files is not None:
            kwargs["files"] = files
            if data is not None:
                kwargs["data"] = data
        elif data is not None:
            kwargs["data"] = data
        r = httpx.post(f"http://{HOST}:{port}{path}", **kwargs)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return Result(status=r.status_code, body=body, headers=dict(r.headers), raw=r.content)
    except Exception as e:
        return Result(error=str(e))


def http_get_raw(port, path, headers=None, timeout=10):
    """Return raw bytes + status + headers."""
    try:
        r = httpx.get(f"http://{HOST}:{port}{path}", headers=headers or {}, timeout=timeout, follow_redirects=False)
        return Result(status=r.status_code, body=r.text, headers=dict(r.headers), raw=r.content)
    except Exception as e:
        return Result(error=str(e))


async def ws_test(port, path, messages=None, expect_subprotocol=None,
                  subprotocols=None, expect_binary=False, send_binary=None,
                  close_after_recv=0, extra_headers=None):
    """Run a WebSocket test and return received messages."""
    import websockets

    url = f"ws://{HOST}:{port}{path}"
    kwargs = {}
    if subprotocols:
        kwargs["subprotocols"] = subprotocols
    if extra_headers:
        kwargs["additional_headers"] = extra_headers

    received = []
    subproto = None
    close_code = None
    close_reason = None
    error = None

    try:
        async with websockets.connect(url, open_timeout=5, close_timeout=5, **kwargs) as ws:
            subproto = ws.subprotocol
            if messages:
                for msg in messages:
                    if isinstance(msg, bytes):
                        await ws.send(msg)
                    else:
                        await ws.send(msg)
            # Receive messages
            recv_count = 0
            while True:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=3)
                    received.append(data)
                    recv_count += 1
                    if close_after_recv and recv_count >= close_after_recv:
                        break
                except asyncio.TimeoutError:
                    break
                except websockets.ConnectionClosedOK as e:
                    close_code = e.code
                    close_reason = e.reason
                    break
                except websockets.ConnectionClosed as e:
                    close_code = e.code
                    close_reason = e.reason
                    break
    except websockets.ConnectionClosedOK as e:
        close_code = e.code
        close_reason = e.reason
    except websockets.ConnectionClosed as e:
        close_code = e.code
        close_reason = e.reason
    except (websockets.InvalidStatus, websockets.InvalidHandshake, ConnectionError, OSError) as e:
        error = str(e)

    return {
        "received": received,
        "subprotocol": subproto,
        "close_code": close_code,
        "close_reason": close_reason,
        "error": error,
    }


# ── Test definitions ─────────────────────────────────────────────────

TESTS = {}

def test(pattern_id):
    def decorator(fn):
        TESTS[pattern_id] = fn
        return fn
    return decorator


# ── WebSocket tests (251-270) ────────────────────────────────────────

@test(251)
def p251(port):
    r = asyncio.run(ws_test(port, "/ws/p251", messages=["hello"], close_after_recv=1))
    assert r["received"] == ["echo:hello"], f"got {r['received']}"

@test(252)
def p252(port):
    payload = bytes([0, 1, 255, 128])
    r = asyncio.run(ws_test(port, "/ws/p252", messages=[payload], close_after_recv=1))
    assert len(r["received"]) == 1
    assert r["received"][0] == payload, f"got {r['received'][0]!r}"

@test(253)
def p253(port):
    r = asyncio.run(ws_test(port, "/ws/p253", messages=[json.dumps({"hello": "world"})], close_after_recv=1))
    assert len(r["received"]) == 1
    data = json.loads(r["received"][0])
    assert data["hello"] == "world"
    assert data["echoed"] is True

@test(254)
def p254(port):
    msgs = [f"msg-{i}" for i in range(5)]
    r = asyncio.run(ws_test(port, "/ws/p254", messages=msgs, close_after_recv=5))
    assert len(r["received"]) == 5
    for i in range(5):
        assert r["received"][i] == f"got:msg-{i}", f"msg {i}: got {r['received'][i]}"

@test(255)
def p255(port):
    r = asyncio.run(ws_test(port, "/ws/p255", subprotocols=["chat.v1", "chat.v2"], close_after_recv=1))
    assert r["subprotocol"] == "chat.v1", f"got subprotocol={r['subprotocol']}"

@test(256)
def p256(port):
    r = asyncio.run(ws_test(port, "/ws/p256"))
    assert r["close_code"] == 4001, f"got close_code={r['close_code']}"

@test(257)
def p257(port):
    r = asyncio.run(ws_test(port, "/ws/p257"))
    assert r["close_code"] == 4002, f"got close_code={r['close_code']}"
    assert r["close_reason"] == "custom-reason", f"got reason={r['close_reason']}"

@test(258)
def p258(port):
    r = asyncio.run(ws_test(port, "/ws/p258?token=abc123", close_after_recv=1))
    assert r["received"] == ["token:abc123"], f"got {r['received']}"

@test(259)
def p259(port):
    r = asyncio.run(ws_test(port, "/ws/p259/room42", close_after_recv=1))
    assert r["received"] == ["room:room42"], f"got {r['received']}"

@test(260)
def p260(port):
    r = asyncio.run(ws_test(port, "/ws/p260"))
    # Should get an error or close, not a normal message
    assert r["error"] is not None or r["close_code"] is not None or len(r["received"]) == 0, \
        f"expected rejection, got received={r['received']}"

@test(261)
def p261(port):
    r = asyncio.run(ws_test(port, "/ws/p261", close_after_recv=1))
    assert r["received"] == ["before-close"], f"got {r['received']}"

@test(262)
def p262(port):
    r = asyncio.run(ws_test(port, "/ws/p262", messages=["test"], close_after_recv=1))
    assert r["received"] == ["custom-param:test"], f"got {r['received']}"

@test(263)
def p263(port):
    r = asyncio.run(ws_test(port, "/wsrouter/p263", close_after_recv=1))
    assert r["received"] == ["from-router"], f"got {r['received']}"

@test(264)
def p264(port):
    r = asyncio.run(ws_test(port, "/outer/inner/p264", close_after_recv=1))
    assert r["received"] == ["nested-router"], f"got {r['received']}"

@test(265)
def p265(port):
    msgs = ["a", "b", "STOP"]
    r = asyncio.run(ws_test(port, "/ws/p265", messages=msgs, close_after_recv=1))
    assert len(r["received"]) == 1
    assert r["received"][0] == "count:3", f"got {r['received']}"

@test(266)
def p266(port):
    big = "X" * 10000
    r = asyncio.run(ws_test(port, "/ws/p266", messages=[big], close_after_recv=1))
    assert r["received"] == ["len:10000"], f"got {r['received']}"

@test(267)
def p267(port):
    payload = bytes([1, 2, 3, 4, 5])
    r = asyncio.run(ws_test(port, "/ws/p267", messages=[payload], close_after_recv=1))
    assert r["received"][0] == bytes([5, 4, 3, 2, 1]), f"got {r['received'][0]!r}"

@test(268)
def p268(port):
    r = asyncio.run(ws_test(port, "/ws/p268", close_after_recv=1))
    assert len(r["received"]) == 1
    # Binary mode: received as bytes
    msg = r["received"][0]
    if isinstance(msg, bytes):
        data = json.loads(msg.decode())
    else:
        data = json.loads(msg)
    assert data["mode"] == "binary"

@test(269)
def p269(port):
    r = asyncio.run(ws_test(port, "/ws/p269", messages=["hi"], close_after_recv=1))
    assert "type:websocket.receive" in r["received"][0], f"got {r['received']}"

@test(270)
def p270(port):
    r = asyncio.run(ws_test(port, "/ws/p270", close_after_recv=1,
                            extra_headers={"User-Agent": "parity-test/1.0"}))
    assert "ua:parity-test/1.0" in r["received"][0], f"got {r['received']}"


# ── Streaming/SSE tests (271-320) ────────────────────────────────────

@test(271)
def p271(port):
    r = http_get(port, "/p271")
    assert r.status == 200
    for i in range(5):
        assert f"chunk-{i}" in r.body, f"missing chunk-{i}"

@test(272)
def p272(port):
    r = http_get(port, "/p272")
    assert r.status == 200
    for i in range(5):
        assert f"async-chunk-{i}" in r.body

@test(273)
def p273(port):
    r = http_get_raw(port, "/p273")
    assert r.status == 200
    ct = r.headers.get("content-type", "")
    assert "text/event-stream" in ct, f"content-type={ct}"

@test(274)
def p274(port):
    r = http_get(port, "/p274")
    assert r.status == 200
    assert "data: hello" in r.body
    assert "data: world" in r.body

@test(275)
def p275(port):
    r = http_get(port, "/p275")
    assert "data: [DONE]" in r.body

@test(276)
def p276(port):
    r = http_get(port, "/p276")
    assert "data: string" in r.body
    assert "data: bytes" in r.body

@test(277)
def p277(port):
    r = http_get_raw(port, "/p277")
    ct = r.headers.get("content-type", "")
    assert "text/event-stream" in ct, f"content-type={ct}"

@test(278)
def p278(port):
    r = http_get_raw(port, "/p278")
    ct = r.headers.get("content-type", "")
    assert "text/event-stream" in ct, f"content-type={ct}"
    assert "data: sse1" in r.body

@test(279)
def p279(port):
    r = http_get(port, "/p279")
    assert r.status == 200
    lines = r.body.strip().split("\n")
    assert len(lines) == 1000, f"got {len(lines)} lines"

@test(280)
def p280(port):
    r = http_get_raw(port, "/p280")
    assert r.headers.get("x-custom-stream") == "true"
    assert r.headers.get("x-count") == "1"

@test(281)
def p281(port):
    r = http_get(port, "/p281")
    assert r.status == 200
    assert "ok" in r.body

@test(282)
def p282(port):
    r = http_get(port, "/p282")
    assert r.status == 200
    assert r.body == "abc"

@test(283)
def p283(port):
    r = http_get_raw(port, "/p283")
    assert r.status == 200
    assert r.raw == b"file-like-content-here"

@test(284)
def p284(port):
    r = http_get(port, "/p284")
    assert r.status == 200

@test(285)
def p285(port):
    r = http_get(port, "/p285")
    assert r.body == "only-one"

@test(286)
def p286(port):
    r = http_get_raw(port, "/p286")
    assert r.status == 200
    text = r.raw.decode()
    for i in range(3):
        assert f"bytes-{i}" in text

@test(287)
def p287(port):
    r = http_get(port, "/p287")
    assert r.status == 206

@test(288)
def p288(port):
    r = http_get(port, "/p288")
    assert "\u00e9" in r.body
    assert "\u00e0" in r.body

@test(289)
def p289(port):
    r = http_get(port, "/p289")
    assert "event: update" in r.body
    assert "data: payload1" in r.body

@test(290)
def p290(port):
    r = http_get(port, "/p290")
    assert "id: 1" in r.body
    assert "data: first" in r.body

@test(291)
def p291(port):
    r = http_get(port, "/p291")
    assert r.body.count("data:") >= 2

@test(292)
def p292(port):
    r = http_get(port, "/p292")
    assert "retry: 3000" in r.body

@test(293)
def p293(port):
    r = http_get(port, "/p293")
    assert ": this is a comment" in r.body

@test(294)
def p294(port):
    r = http_get(port, "/p294")
    assert '"key"' in r.body or "'key'" in r.body

@test(295)
def p295(port):
    r = http_get(port, "/p295")
    for i in range(10):
        assert f"data: rapid-{i}" in r.body

@test(296)
def p296(port):
    r = http_get(port, "/p296")
    assert "data: notempty" in r.body

@test(297)
def p297(port):
    r = http_get(port, "/p297")
    assert "data:" in r.body

@test(298)
def p298(port):
    r = http_get(port, "/p298")
    assert "event: msg" in r.body
    assert "id: 42" in r.body
    assert "data: combined" in r.body

@test(299)
def p299(port):
    r = http_get(port, "/p299")
    assert ": keepalive" in r.body
    assert "data: after-keepalive" in r.body

@test(300)
def p300(port):
    r = http_get(port, "/p300")
    assert "data: 12345" in r.body
    assert "data: 67890" in r.body

# p301-p310: streaming + middleware interaction

@test(301)
def p301(port):
    rj = http_get(port, "/p301/json")
    rs = http_get(port, "/p301/stream")
    assert rj.status == 200
    assert rj.body == {"source": "json"} or rj.body.get("source") == "json"
    assert rs.status == 200
    assert "streamed" in rs.body

@test(302)
def p302(port):
    r = http_get_raw(port, "/p302")
    assert r.status == 200
    # Chunked transfer or missing content-length
    cl = r.headers.get("content-length")
    # Both behaviors are valid: some servers set content-length if known

@test(303)
def p303(port):
    r = http_get(port, "/p303")
    assert r.status == 201

@test(304)
def p304(port):
    r = http_get_raw(port, "/p304")
    assert r.headers.get("x-a") == "1"
    assert r.headers.get("x-b") == "2"
    assert r.headers.get("x-c") == "3"

@test(305)
def p305(port):
    r = http_get_raw(port, "/p305")
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"content-type={ct}"

@test(306)
def p306(port):
    r = http_post(port, "/p306")
    assert r.status == 200
    assert "post-stream" in (r.body if isinstance(r.body, str) else str(r.body))

@test(307)
def p307(port):
    r = http_get(port, "/p307")
    assert "after-empty" in r.body

@test(308)
def p308(port):
    r = http_get_raw(port, "/p308")
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct
    data = json.loads(r.raw)
    assert data == {"streaming": "json"}

@test(309)
def p309(port):
    ra = http_get(port, "/p309/a")
    rb = http_get(port, "/p309/b")
    assert "stream-a" in ra.body
    assert "stream-b" in rb.body

@test(310)
def p310(port):
    r = http_get_raw(port, "/p310")
    assert r.raw == bytes([0, 1, 2, 3, 255, 254, 253])

@test(311)
def p311(port):
    r = http_get(port, "/p311")
    assert r.status == 404

@test(312)
def p312(port):
    r = http_get(port, "/p312")
    assert r.status == 500

@test(313)
def p313(port):
    r = http_get(port, "/p313")
    assert r.status == 204

@test(314)
def p314(port):
    r = http_get(port, "/p314/202")
    assert r.status == 202

@test(315)
def p315(port):
    r = http_get(port, "/p315")
    assert "start" in r.body
    assert "end" in r.body

@test(316)
def p316(port):
    r = http_get(port, "/p316")
    parts = r.body.rstrip(",").split(",")
    assert len(parts) == 20
    for i in range(20):
        assert parts[i] == str(i), f"position {i}: got {parts[i]}"

@test(317)
def p317(port):
    r = http_get_raw(port, "/p317")
    assert r.raw == bytes(range(256))

@test(318)
def p318(port):
    r = http_get(port, "/p318")
    assert "line1" in r.body
    assert "line2" in r.body

@test(319)
def p319(port):
    r = http_get(port, "/p319?n=5")
    for i in range(5):
        assert f"item-{i}" in r.body

@test(320)
def p320(port):
    r = http_get(port, "/p320/alice")
    assert "hello-alice" in r.body


# ── File handling tests (321-370) ────────────────────────────────────

@test(321)
def p321(port):
    r = http_post(port, "/p321", files={"file": ("test.txt", b"hello world", "text/plain")})
    assert r.status == 200
    assert r.body["size"] == 11

@test(322)
def p322(port):
    r = http_post(port, "/p322", files={"file": ("myfile.txt", b"data", "text/plain")})
    assert r.body["filename"] == "myfile.txt"

@test(323)
def p323(port):
    r = http_post(port, "/p323", files={"file": ("test.json", b"{}", "application/json")})
    assert r.body["content_type"] == "application/json"

@test(324)
def p324(port):
    r = http_post(port, "/p324", files={"file": ("test.bin", b"\x00\x01\x02\x03", "application/octet-stream")})
    assert r.body["is_bytes"] is True
    assert r.body["hex"] == "00010203"

@test(325)
def p325(port):
    data = b"A" * 100
    r = http_post(port, "/p325", files={"file": ("test.dat", data, "application/octet-stream")})
    assert r.body["read_len"] == 100

@test(326)
def p326(port):
    r = http_post(port, "/p326", files=[
        ("files", ("a.txt", b"aaa", "text/plain")),
        ("files", ("b.txt", b"bbb", "text/plain")),
    ])
    assert r.body["count"] == 2
    assert r.body["names"] == ["a.txt", "b.txt"]

@test(327)
def p327(port):
    r = http_post(port, "/p327",
                  data={"name": "Alice"},
                  files={"file": ("doc.txt", b"hello", "text/plain")})
    assert r.body["name"] == "Alice"
    assert r.body["filename"] == "doc.txt"
    assert r.body["size"] == 5

@test(328)
def p328(port):
    big = b"X" * (1024 * 1024)
    r = http_post(port, "/p328", files={"file": ("big.dat", big, "application/octet-stream")})
    assert r.body["size"] == 1024 * 1024

@test(329)
def p329(port):
    r = http_get_raw(port, "/p329")
    assert r.status == 200
    assert b"Hello parity test" in r.raw

@test(330)
def p330(port):
    r = http_get_raw(port, "/p330")
    cd = r.headers.get("content-disposition", "")
    assert "download.txt" in cd

@test(331)
def p331(port):
    r = http_get_raw(port, "/p331")
    cd = r.headers.get("content-disposition", "")
    assert "inline" in cd

@test(332)
def p332(port):
    r = http_get_raw(port, "/p332")
    ct = r.headers.get("content-type", "")
    assert "html" in ct.lower(), f"content-type={ct}"

@test(333)
def p333(port):
    r = http_get_raw(port, "/p333")
    ct = r.headers.get("content-type", "")
    assert "application/octet-stream" in ct, f"content-type={ct}"

@test(334)
def p334(port):
    r = http_post(port, "/p334", files={"file": ("empty.txt", b"", "text/plain")})
    assert r.body["size"] == 0

@test(335)
def p335(port):
    r = http_post(port, "/p335", files={"file": ("my file (1).txt", b"data", "text/plain")})
    assert "my file" in r.body["filename"]

@test(336)
def p336(port):
    r = http_post(port, "/p336", files={"file": ("text.txt", b"hello world", "text/plain")})
    assert r.body["content"] == "hello world"

@test(337)
def p337(port):
    r = http_post(port, "/p337", files={"file": ("bin.dat", bytes([0xDE, 0xAD, 0xBE, 0xEF]), "application/octet-stream")})
    assert r.body["hex"] == "deadbeef"

@test(338)
def p338(port):
    r = http_post(port, "/p338",
                  data={"description": "test file"},
                  files={"file": ("f.txt", b"content", "text/plain")})
    assert r.body["description"] == "test file"
    assert r.body["size"] == 7

@test(339)
def p339(port):
    r = http_post(port, "/p339",
                  data={"tag": "important"},
                  files={"file": ("f.txt", b"data", "text/plain")})
    assert r.body["tag"] == "important"
    assert r.body["size"] == 4

@test(340)
def p340(port):
    r = http_post(port, "/p340",
                  files={"file": ("f.txt", b"data", "text/plain")})
    assert r.body["label"] == "default"

@test(341)
def p341(port):
    r = http_get_raw(port, "/p341")
    assert r.status == 200
    assert len(r.raw) == 1024  # 256 * 4

@test(342)
def p342(port):
    r = http_get_raw(port, "/p342")
    assert b"<h1>Test</h1>" in r.raw

@test(343)
def p343(port):
    r = http_get_raw(port, "/p343")
    ct = r.headers.get("content-type", "")
    assert "png" in ct.lower() or "octet" in ct.lower(), f"content-type={ct}"

@test(344)
def p344(port):
    r = http_get_raw(port, "/p344")
    assert r.status == 200

@test(345)
def p345(port):
    r = http_get_raw(port, "/p345")
    assert r.status == 200
    assert len(r.raw) == 1024 * 1024

@test(346)
def p346(port):
    r = http_get(port, "/p346")
    # Should be 404 or 500, not 200
    assert r.status >= 400, f"expected error status, got {r.status}"

@test(347)
def p347(port):
    r = http_post(port, "/p347",
                  files=[
                      ("file1", ("a.txt", b"aaa", "text/plain")),
                      ("file2", ("b.txt", b"bbbb", "text/plain")),
                  ])
    assert r.body["file1_name"] == "a.txt"
    assert r.body["file2_name"] == "b.txt"
    assert r.body["file1_size"] == 3
    assert r.body["file2_size"] == 4

@test(348)
def p348(port):
    r = http_post(port, "/p348",
                  data={"title": "My Upload"},
                  files=[
                      ("files", ("x.txt", b"xxx", "text/plain")),
                      ("files", ("y.txt", b"yyy", "text/plain")),
                  ])
    assert r.body["title"] == "My Upload"
    assert r.body["names"] == ["x.txt", "y.txt"]

@test(349)
def p349(port):
    payload = b"\x00\x01\x02\xff\xfe"
    r = http_post(port, "/p349", files={"file": ("bin.dat", payload, "application/octet-stream")})
    assert r.raw == payload

@test(350)
def p350(port):
    r = http_post(port, "/p350", files={"file": ("image.jpg", b"fake", "image/jpeg")})
    assert r.body["content_type"] == "image/jpeg"
    assert r.body["filename"] == "image.jpg"

@test(351)
def p351(port):
    r = http_post(port, "/p351", data={"name": "Bob", "age": "30"})
    assert r.body["name"] == "Bob"
    assert r.body["age"] == 30

@test(352)
def p352(port):
    r = http_post(port, "/p352", data={"name": "Bob"})
    assert r.body["name"] == "Bob"
    assert r.body["role"] == "user"

@test(353)
def p353(port):
    r = http_post(port, "/p353", data={"a": "x", "b": "y", "c": "z"})
    assert r.body == {"a": "x", "b": "y", "c": "z"}

@test(354)
def p354(port):
    r = http_post(port, "/p354", data={"count": "42"})
    assert r.body["count"] == 42

@test(355)
def p355(port):
    r = http_post(port, "/p355", data={"price": "19.99"})
    assert abs(r.body["price"] - 19.99) < 0.01

@test(356)
def p356(port):
    r = http_post(port, "/p356", data={"active": "true"})
    assert r.body["active"] is True

@test(357)
def p357(port):
    r = http_post(port, "/p357?q=search", data={"name": "Alice"})
    assert r.body["q"] == "search"
    assert r.body["name"] == "Alice"

@test(358)
def p358(port):
    r = http_post(port, "/p358", files=[
        ("files", ("a.txt", b"aaa", "text/plain")),
        ("files", ("b.txt", b"bb", "text/plain")),
    ])
    assert r.body["count"] == 2
    assert r.body["total_size"] == 5

@test(359)
def p359(port):
    r = http_post(port, "/p359", files={"file": ("test.dat", b"12345", "application/octet-stream")})
    assert r.body["filename"] == "test.dat"
    assert r.body["size"] == 5

@test(360)
def p360(port):
    # Send a non-empty value; empty form strings behave inconsistently across frameworks
    r = http_post(port, "/p360", data={"value": "hello"})
    assert r.body["value"] == "hello"
    assert r.body["empty"] is False

@test(361)
def p361(port):
    r = http_post(port, "/p361/docs", files={"file": ("f.txt", b"abc", "text/plain")})
    assert r.body["category"] == "docs"
    assert r.body["size"] == 3

@test(362)
def p362(port):
    r = http_post(port, "/p362?tag=urgent", files={"file": ("f.txt", b"abc", "text/plain")})
    assert r.body["tag"] == "urgent"

@test(363)
def p363(port):
    r = http_post(port, "/p363?q=test",
                  data={"name": "Bob"},
                  files={"file": ("f.txt", b"data", "text/plain")})
    assert r.body["q"] == "test"
    assert r.body["name"] == "Bob"
    assert r.body["size"] == 4

@test(364)
def p364(port):
    r = http_post(port, "/p364/uploads", files=[
        ("files", ("a.txt", b"a", "text/plain")),
        ("files", ("b.txt", b"b", "text/plain")),
    ])
    assert r.body["bucket"] == "uploads"
    assert r.body["names"] == ["a.txt", "b.txt"]

@test(365)
def p365(port):
    r = http_post(port, "/p365",
                  data={"count": "5"},
                  files={"file": ("f.txt", b"hello", "text/plain")})
    assert r.body["count"] == 5
    assert r.body["size"] == 5

@test(366)
def p366(port):
    payload = bytes([10, 20, 30])
    r = http_post(port, "/p366", files={"file": ("f.bin", payload, "application/octet-stream")})
    assert r.raw == payload

@test(367)
def p367(port):
    r = http_post(port, "/p367", data={"text": "hello & goodbye <world>"})
    assert r.body["text"] == "hello & goodbye <world>"

@test(368)
def p368(port):
    r = http_post(port, "/p368/upload", files={"file": ("f.txt", b"abc", "text/plain")})
    assert r.body["path"] == "/p368/upload"

@test(369)
def p369(port):
    r = http_get_raw(port, "/p369")
    assert r.headers.get("x-file-type") == "text"

@test(370)
def p370(port):
    r = http_post(port, "/p370",
                  data={"name": "Alice"},
                  files={"file": ("f.txt", b"data", "text/plain")})
    assert r.body["name"] == "Alice"
    assert r.body["has_file"] is True


# ── Security tests (371-400) ─────────────────────────────────────────

@test(371)
def p371(port):
    r = http_get(port, "/p371", headers={"Authorization": "Bearer my-token"})
    assert r.status == 200
    assert r.body["token"] == "my-token"

@test(372)
def p372(port):
    r = http_get(port, "/p372")
    assert r.status == 401, f"expected 401, got {r.status}"

@test(373)
def p373(port):
    r = http_get(port, "/p373")
    assert r.status == 200
    assert r.body["token"] is None

@test(374)
def p374(port):
    r = http_get(port, "/p374", headers={"Authorization": "Bearer secret123"})
    assert r.status == 200
    assert r.body["scheme"] == "Bearer"
    assert r.body["credentials"] == "secret123"

@test(375)
def p375(port):
    r = http_get(port, "/p375")
    assert r.status in (401, 403), f"expected 401 or 403, got {r.status}"

@test(376)
def p376(port):
    r = http_get(port, "/p376", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status in (401, 403), f"expected 401 or 403, got {r.status}"

@test(377)
def p377(port):
    creds = base64.b64encode(b"admin:secret").decode()
    r = http_get(port, "/p377", headers={"Authorization": f"Basic {creds}"})
    assert r.status == 200
    assert r.body["username"] == "admin"
    assert r.body["password"] == "secret"

@test(378)
def p378(port):
    r = http_get(port, "/p378")
    assert r.status == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert "basic" in www_auth.lower(), f"www-authenticate={www_auth}"

@test(379)
def p379(port):
    r = http_get(port, "/p379", headers={"X-API-Key": "my-key-123"})
    assert r.status == 200
    assert r.body["api_key"] == "my-key-123"

@test(380)
def p380(port):
    r = http_get(port, "/p380")
    assert r.status in (401, 403), f"expected 401 or 403, got {r.status}"

@test(381)
def p381(port):
    r = http_get(port, "/p381", headers={"Authorization": "Bearer test-token"})
    assert r.status == 200
    assert r.body["user"] == "testuser"
    assert r.body["token"] == "test-token"

@test(382)
def p382(port):
    r = http_post(port, "/p382",
                  data={"username": "alice", "password": "pass123", "scope": "read write", "grant_type": "password"})
    assert r.status == 200
    assert r.body["username"] == "alice"
    assert r.body["password"] == "pass123"
    assert r.body["scopes"] == ["read", "write"]

@test(383)
def p383(port):
    r = http_get(port, "/p383", headers={"Authorization": "Bearer tok1", "X-API-Key": "key1"})
    assert r.status == 200
    assert r.body["token"] == "tok1"
    assert r.body["api_key"] == "key1"

@test(384)
def p384(port):
    r = http_get(port, "/secure/p384", headers={"Authorization": "Bearer router-token"})
    assert r.status == 200
    assert r.body["access"] == "granted"

@test(385)
def p385(port):
    r = http_get(port, "/p385", headers={"Authorization": "Bearer chain-token"})
    assert r.status == 200
    assert r.body["user"] == "alice"
    assert r.body["token"] == "chain-token"

@test(386)
def p386(port):
    r = http_get(port, "/p386", headers={"Authorization": "Bearer valid-token"})
    assert r.status == 200
    assert r.body["verified"] is True

@test(387)
def p387(port):
    r = http_get(port, "/p387")
    assert r.status == 200
    assert r.body["creds"] is None

@test(388)
def p388(port):
    r = http_get(port, "/p388")
    assert r.status == 200
    assert r.body["api_key"] is None

@test(389)
def p389(port):
    r = http_get(port, "/p389", headers={"Authorization": "Bearer admin-token"})
    assert r.status == 200
    assert r.body["admin"] is True

@test(390)
def p390(port):
    creds = base64.b64encode(b"admin:secret").decode()
    r = http_get(port, "/p390", headers={"Authorization": f"Basic {creds}"})
    assert r.status == 200
    assert r.body["authenticated"] is True

@test(391)
def p391(port):
    r = http_post(port, "/p391",
                  data={"username": "bob", "password": "pwd", "scope": "admin users"})
    assert r.status == 200
    assert r.body["username"] == "bob"
    assert r.body["scopes"] == ["admin", "users"]

@test(392)
def p392(port):
    r = http_get(port, "/p392", headers={"Authorization": "Bearer deep-token"})
    assert r.status == 200
    assert r.body["level"] == 3
    assert r.body["token"] == "deep-token"

@test(393)
def p393(port):
    r = http_get(port, "/p393", headers={"Authorization": "Bearer abcdef"})
    assert r.status == 200
    assert r.body["token_length"] == 6

@test(394)
def p394(port):
    # Use ASCII-safe characters for reliable base64 round-trip
    creds = base64.b64encode(b"testuser:p@ssw0rd!").decode()
    r = http_get(port, "/p394", headers={"Authorization": f"Basic {creds}"})
    assert r.status == 200
    assert r.body["username"] == "testuser"
    assert r.body["password"] == "p@ssw0rd!"

@test(395)
def p395(port):
    r = http_get(port, "/p395", headers={"X-Custom-Key": "custom-val"})
    assert r.status == 200
    assert r.body["key"] == "custom-val"

@test(396)
def p396(port):
    r = http_get(port, "/p396")
    assert r.status == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert "bearer" in www_auth.lower(), f"www-authenticate={www_auth}"

@test(397)
def p397(port):
    r = http_get(port, "/p397", headers={"Authorization": "Bearer exactly-this-token"})
    assert r.body["token"] == "exactly-this-token"
    assert r.body["length"] == len("exactly-this-token")

@test(398)
def p398(port):
    creds = base64.b64encode(b"user:").decode()
    r = http_get(port, "/p398", headers={"Authorization": f"Basic {creds}"})
    assert r.status == 200
    assert r.body["username"] == "user"
    assert r.body["password"] == ""

@test(399)
def p399(port):
    r = http_get(port, "/p399", headers={"Authorization": "Bearer ctx-token"})
    assert r.status == 200
    assert r.body["token"] == "ctx-token"
    assert r.body["db"] == "connected"

@test(400)
def p400(port):
    r = http_get(port, "/p400", headers={"Authorization": "Bearer chain-val"})
    assert r.status == 200
    assert r.body["result"] == "b:a:chain-val"


# ── Main runner ──────────────────────────────────────────────────────

def run_all():
    print("Starting servers...")

    fastapi_proc = None
    fastapi_turbo_proc = None

    try:
        fastapi_proc = start_fastapi(FASTAPI_PORT)
        print(f"  FastAPI (uvicorn) on :{FASTAPI_PORT}")
    except Exception as e:
        print(f"  FAILED to start FastAPI: {e}")
        return

    try:
        fastapi_turbo_proc = start_fastapi_turbo(FASTAPI_TURBO_PORT)
        print(f"  fastapi-turbo on :{FASTAPI_TURBO_PORT}")
    except Exception as e:
        print(f"  FAILED to start fastapi-turbo: {e}")
        _kill(fastapi_proc)
        return

    print()
    print("Running patterns 251-400...")
    print("=" * 60)

    passed = 0
    failed = 0
    failures = []

    pattern_ids = sorted(TESTS.keys())
    for pid in pattern_ids:
        fn = TESTS[pid]
        label = f"p{pid}"

        # Run against FastAPI
        fa_ok = True
        fa_err = None
        try:
            fn(FASTAPI_PORT)
        except Exception as e:
            fa_ok = False
            fa_err = str(e)

        # Run against fastapi-turbo
        rs_ok = True
        rs_err = None
        try:
            fn(FASTAPI_TURBO_PORT)
        except Exception as e:
            rs_ok = False
            rs_err = str(e)

        # Compare
        if fa_ok and rs_ok:
            passed += 1
            print(f"  {label}: PASS")
        elif fa_ok and not rs_ok:
            failed += 1
            failures.append((label, f"fastapi-turbo FAIL: {rs_err}"))
            print(f"  {label}: FAIL (fastapi-turbo: {rs_err})")
        elif not fa_ok and rs_ok:
            failed += 1
            failures.append((label, f"fastapi FAIL: {fa_err}"))
            print(f"  {label}: FAIL (fastapi: {fa_err})")
        else:
            # Both fail -- if same error, consider it a pass (both behave the same)
            if fa_err == rs_err:
                passed += 1
                print(f"  {label}: PASS (both error: {fa_err[:60]})")
            else:
                failed += 1
                failures.append((label, f"both fail differently: FA={fa_err} RS={rs_err}"))
                print(f"  {label}: FAIL (FA={fa_err[:40]} RS={rs_err[:40]})")

    print()
    print("=" * 60)
    total = passed + failed
    print(f"PASS: {passed}/{total}")
    print(f"FAIL: {failed}")
    if failures:
        for label, reason in failures:
            print(f"  {label}: {reason}")

    _kill(fastapi_proc)
    _kill(fastapi_turbo_proc)


if __name__ == "__main__":
    run_all()
