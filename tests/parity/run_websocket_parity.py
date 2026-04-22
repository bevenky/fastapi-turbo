"""FastAPI vs fastapi-rs WebSocket parity runner.

Boots both servers on their own ports from the SAME `parity_app_websocket`
module and compares the observable client-side behaviour of each scenario
(payload, close code, close reason, chosen subprotocol, etc.).

Run:
    python tests/parity/run_websocket_parity.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

HOST = "127.0.0.1"


# ── Colours (avoid pulling another dep) ──────────────────────────────
GRN = "\033[92m"
RED = "\033[91m"
CYN = "\033[96m"
YEL = "\033[93m"
BLD = "\033[1m"
RST = "\033[0m"


@dataclass
class Result:
    tid: int
    desc: str
    ok: bool
    detail: str = ""


def _free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ws(port: int, path: str = "/ws/echo", timeout: float = 15.0) -> bool:
    # Probe via TCP first — it comes up faster than a full WS handshake
    # and avoids dozens of "handshake failed" warnings while we wait.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket()
            s.settimeout(0.3)
            s.connect((HOST, port))
            s.close()
            break
        except Exception:
            time.sleep(0.15)
    else:
        return False
    # Now verify the WS path actually handshakes.
    url = f"ws://{HOST}:{port}{path}"
    while time.time() < deadline:
        try:
            async def probe() -> None:
                async with websockets.connect(url, open_timeout=2.0) as ws:
                    await ws.close()
            asyncio.run(probe())
            return True
        except Exception:
            time.sleep(0.15)
    return False


def _start_uvicorn(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "parity_app_websocket:app",
         "--host", HOST, "--port", str(port),
         "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _start_fastapi_rs(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    code = f"""
import fastapi_rs.compat
fastapi_rs.compat.install()
import sys
sys.path.insert(0, {HERE!r})
from parity_app_websocket import app
app.run({HOST!r}, {port})
"""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ── Individual scenarios ─────────────────────────────────────────────
async def scenario_echo_text(base_url: str) -> dict[str, Any]:
    async with websockets.connect(f"{base_url}/ws/echo") as ws:
        await ws.send("hello")
        r1 = await ws.recv()
        await ws.send("world")
        r2 = await ws.recv()
    return {"r1": r1, "r2": r2}


async def scenario_echo_bytes(base_url: str) -> dict[str, Any]:
    async with websockets.connect(f"{base_url}/ws/echo-bytes") as ws:
        payload = bytes(range(256))
        await ws.send(payload)
        r = await ws.recv()
    return {"r": r.hex()}


async def scenario_echo_json(base_url: str) -> dict[str, Any]:
    async with websockets.connect(f"{base_url}/ws/echo-json") as ws:
        await ws.send(json.dumps({"name": "bob", "n": 3}))
        r1 = json.loads(await ws.recv())
        await ws.send(json.dumps([1, 2, 3]))
        r2 = json.loads(await ws.recv())
    return {"r1": r1, "r2": r2}


async def scenario_close_code_reason(base_url: str) -> dict[str, Any]:
    async with websockets.connect(f"{base_url}/ws/once") as ws:
        payload = await ws.recv()
        try:
            await ws.recv()
        except websockets.ConnectionClosed as e:
            return {"payload": payload, "code": e.rcvd.code, "reason": e.rcvd.reason}
    return {"payload": payload, "code": None, "reason": None}


async def scenario_subprotocol(base_url: str) -> dict[str, Any]:
    async with websockets.connect(
        f"{base_url}/ws/subproto",
        subprotocols=["chat.v1", "chat.v2"],
    ) as ws:
        body = json.loads(await ws.recv())
        chosen = ws.subprotocol
    return {"server_chosen": body["chosen"], "negotiated": chosen,
            "offered": body["offered"]}


async def scenario_scope(base_url: str) -> dict[str, Any]:
    import websockets.asyncio.client as wsc
    async with wsc.connect(
        f"{base_url}/ws/scope/alice?n=42",
        additional_headers={"X-Custom": "val-here", "Cookie": "session=abc"},
    ) as ws:
        return json.loads(await ws.recv())


async def scenario_app_state(base_url: str) -> dict[str, Any]:
    # Two connections to verify state carries across requests on the same app.
    counters = []
    for _ in range(2):
        async with websockets.connect(f"{base_url}/ws/state") as ws:
            counters.append(json.loads(await ws.recv())["counter"])
    return {"counters": counters}


async def scenario_reject_before_accept(base_url: str) -> dict[str, Any]:
    try:
        async with websockets.connect(f"{base_url}/ws/raise"):
            pass
        return {"result": "connected (unexpected)", "code": None, "reason": None}
    except websockets.InvalidStatus as e:
        # HTTP-level 403 if raised before accept in the HTTP handshake path.
        return {"result": "http_reject", "status": e.response.status_code}
    except websockets.ConnectionClosed as e:
        return {"result": "ws_close", "code": e.rcvd.code, "reason": e.rcvd.reason}


async def scenario_dep_reject(base_url: str) -> dict[str, Any]:
    # Missing X-Token → auth_dep raises WebSocketException(4401).
    try:
        async with websockets.connect(f"{base_url}/ws/with-dep"):
            pass
    except websockets.InvalidStatus as e:
        return {"result": "http_reject", "status": e.response.status_code}
    except websockets.ConnectionClosed as e:
        return {"result": "ws_close", "code": e.rcvd.code, "reason": e.rcvd.reason}
    return {"result": "connected"}


async def scenario_dep_pass(base_url: str) -> dict[str, Any]:
    import websockets.asyncio.client as wsc
    async with wsc.connect(
        f"{base_url}/ws/with-dep",
        additional_headers={"X-Token": "tok-xyz"},
    ) as ws:
        return json.loads(await ws.recv())


async def scenario_router_prefix(base_url: str) -> dict[str, Any]:
    async with websockets.connect(f"{base_url}/api/chat") as ws:
        welcome = await ws.recv()
        await ws.send("ping")
        ack = await ws.recv()
    return {"welcome": welcome, "ack": ack}


SCENARIOS: list[tuple[str, Callable[[str], Awaitable[dict[str, Any]]]]] = [
    ("echo/text", scenario_echo_text),
    ("echo/bytes", scenario_echo_bytes),
    ("echo/json", scenario_echo_json),
    ("close-code+reason", scenario_close_code_reason),
    ("subprotocol-negotiation", scenario_subprotocol),
    ("scope: path/query/header/cookie", scenario_scope),
    ("app.state across requests", scenario_app_state),
    ("reject-before-accept", scenario_reject_before_accept),
    ("dep-reject (missing token)", scenario_dep_reject),
    ("dep-pass (valid token)", scenario_dep_pass),
    ("router-prefix (/api/chat)", scenario_router_prefix),
]


# ── Runner ───────────────────────────────────────────────────────────
async def _run_one(name: str, fn, fa_url: str, fr_url: str) -> Result:
    try:
        fa = await fn(fa_url)
    except Exception as e:
        return Result(0, name, False, f"FA raised {type(e).__name__}: {e}")
    try:
        fr = await fn(fr_url)
    except Exception as e:
        return Result(0, name, False, f"FR raised {type(e).__name__}: {e}")

    # Normalise transient counters: the /ws/state scenario starts from
    # whatever counter the server last held. Compare *structure* instead
    # of absolute values.
    if name == "app.state across requests":
        ok = (
            isinstance(fa.get("counters"), list)
            and isinstance(fr.get("counters"), list)
            and len(fa["counters"]) == len(fr["counters"]) == 2
            and fa["counters"][1] - fa["counters"][0] == fr["counters"][1] - fr["counters"][0] == 1
        )
        return Result(0, name, ok, f"FA={fa} FR={fr}")

    ok = fa == fr
    return Result(0, name, ok, f"FA={fa} FR={fr}" if not ok else "")


def main() -> int:
    fa_port = _free_port()
    fr_port = _free_port()

    print(f"{BLD}={'=' * 70}")
    print(f"  WebSocket Parity: FA :{fa_port}  |  fastapi-rs :{fr_port}")
    print(f"={'=' * 70}{RST}\n")

    print("Starting uvicorn (FA)...", end=" ", flush=True)
    fa_proc = _start_uvicorn(fa_port)
    print("ok")
    print("Starting fastapi-rs...", end=" ", flush=True)
    fr_proc = _start_fastapi_rs(fr_port)
    print("ok")

    try:
        if not _wait_ws(fa_port):
            print(f"{RED}FA failed to start{RST}")
            return 2
        if not _wait_ws(fr_port):
            print(f"{RED}fastapi-rs failed to start{RST}")
            return 2

        fa_url = f"ws://{HOST}:{fa_port}"
        fr_url = f"ws://{HOST}:{fr_port}"

        results: list[Result] = []
        for i, (name, fn) in enumerate(SCENARIOS, start=1):
            r = asyncio.run(_run_one(name, fn, fa_url, fr_url))
            r.tid = i
            marker = f"{GRN}PASS{RST}" if r.ok else f"{RED}FAIL{RST}"
            print(f"  [{marker}] T{r.tid:02d} {name}")
            if not r.ok:
                print(f"         {r.detail}")
            results.append(r)

        passed = sum(1 for r in results if r.ok)
        total = len(results)
        colour = GRN if passed == total else RED
        print()
        print(f"{BLD}RESULTS: {total} tests | {colour}{passed} PASS{RST}{BLD} | "
              f"{RED}{total - passed} FAIL{RST}")
        return 0 if passed == total else 1
    finally:
        for p in (fa_proc, fr_proc):
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
