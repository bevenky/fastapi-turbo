"""WebSocket parity app — same source used by both stock FastAPI and
fastapi-turbo. Exercises the WS surface most real apps touch: send/receive
text/bytes/json, subprotocols, close codes, path/query/header/cookie
access from ``WebSocket``, and exception propagation.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.exceptions import WebSocketException


app = FastAPI()


@app.websocket("/ws/echo")
async def ws_echo(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/echo-bytes")
async def ws_echo_bytes(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            b = await ws.receive_bytes()
            await ws.send_bytes(b)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/echo-json")
async def ws_echo_json(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            j = await ws.receive_json()
            await ws.send_json({"echo": j, "kind": type(j).__name__})
    except WebSocketDisconnect:
        return


@app.websocket("/ws/once")
async def ws_once(ws: WebSocket) -> None:
    """Accept, send a single payload, close with explicit code + reason."""
    await ws.accept()
    await ws.send_text("hello")
    await ws.close(code=4201, reason="goodbye")


@app.websocket("/ws/subproto")
async def ws_subproto(ws: WebSocket) -> None:
    """Accept the FIRST offered subprotocol the client advertises,
    echoing the choice back in-band so the test can verify it.
    """
    offered = ws.scope.get("subprotocols") or []
    chosen = offered[0] if offered else None
    await ws.accept(subprotocol=chosen)
    await ws.send_text(json.dumps({"chosen": chosen, "offered": offered}))
    await ws.close()


@app.websocket("/ws/scope/{who}")
async def ws_scope(ws: WebSocket, who: str) -> None:
    """Path + query + header + cookie introspection."""
    await ws.accept()
    await ws.send_json(
        {
            "who": who,
            "qs_n": ws.query_params.get("n"),
            "x_custom": ws.headers.get("x-custom"),
            "cookie_session": ws.cookies.get("session"),
            "path": ws.url.path,
        }
    )
    await ws.close()


@app.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    """Verify ws.app.state carries across the request — needed for
    lifespan-initialised resources (DB pools, caches)."""
    await ws.accept()
    counter = getattr(ws.app.state, "ws_counter", 0) + 1
    ws.app.state.ws_counter = counter
    await ws.send_json({"counter": counter})
    await ws.close()


@app.websocket("/ws/raise")
async def ws_raise(ws: WebSocket) -> None:
    """Raise WebSocketException BEFORE accept — Starlette's normative
    path to reject an incoming connection with a custom code.
    """
    raise WebSocketException(code=4403, reason="nope")


# ── Dependencies ─────────────────────────────────────────────────────
def auth_dep(ws: WebSocket) -> str:
    token = ws.headers.get("x-token")
    if not token:
        # Starlette: raising WebSocketException before accept == reject.
        raise WebSocketException(code=4401, reason="missing token")
    return token


@app.websocket("/ws/with-dep")
async def ws_with_dep(ws: WebSocket, token: str = Depends(auth_dep)) -> None:
    await ws.accept()
    await ws.send_json({"token": token})
    await ws.close()


# ── Router inclusion ────────────────────────────────────────────────
ws_router = APIRouter(prefix="/api")


@ws_router.websocket("/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text("welcome")
    try:
        m = await ws.receive_text()
        await ws.send_text(f"ack:{m}")
    except WebSocketDisconnect:
        return
    await ws.close()


app.include_router(ws_router)


if __name__ == "__main__":  # pragma: no cover
    import sys
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 29930
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
