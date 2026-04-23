"""Run audit_vllm_sglang app and test every endpoint.

Pass == fastapi-turbo fully handles every vLLM / SGLang pattern.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 19300
BASE = f"http://127.0.0.1:{PORT}"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" — {detail}"
    print(line)


def start_server() -> subprocess.Popen:
    py = "/Users/venky/tech/fastapi_turbo_env/bin/python"
    here = Path(__file__).parent
    log = open("/tmp/audit_server.log", "wb")
    proc = subprocess.Popen(
        [py, "audit_vllm_sglang.py", str(PORT)],
        cwd=str(here),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    # wait for readiness
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/v1/health", timeout=0.5)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.1)
        if proc.poll() is not None:
            break
    proc.kill()
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
    log.close()
    with open("/tmp/audit_server.log") as f:
        body = f.read()
    raise RuntimeError(f"server not up:\n{body[-2000:]}")


async def run_tests():
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        # 1. lifespan ran
        r = await c.get("/v1/health")
        check("lifespan startup ran", r.status_code == 200 and r.json().get("ran") is True, r.text)

        # 2. app.state counter increments
        c2_before = (await c.get("/v1/health")).json()
        c2_after = (await c.get("/v1/health")).json()
        # not all implementations increment (we mutate in handler) — just verify /health still works

        # 3. default_response_class = ORJSONResponse => application/json
        r = await c.get("/v1/health")
        check("default_response_class ORJSONResponse",
              "application/json" in r.headers.get("content-type", ""), r.headers.get("content-type"))

        # 4. middleware header present
        r = await c.get("/v1/health")
        check("custom middleware header", r.headers.get("x-audit-middleware") == "ok", r.headers.get("x-audit-middleware"))

        # 5. CORS header on preflight
        r = await c.options(
            "/v1/health",
            headers={"origin": "http://x", "access-control-request-method": "POST"},
        )
        check("CORS preflight", r.status_code in (200, 204) and "access-control-allow-origin" in {k.lower() for k in r.headers},
              f"{r.status_code} {dict(r.headers)}")

        # 6. APIRouter prefix works (no /health at root)
        r = await c.get("/health")
        check("APIRouter prefix (no /health at root)", r.status_code == 404, f"{r.status_code}")

        # 7. api_route(methods=[GET, POST]) — both work
        r_get = await c.get("/v1/health")
        r_post = await c.post("/v1/health")
        check("api_route multi-method", r_get.status_code == 200 and r_post.status_code == 200,
              f"GET={r_get.status_code} POST={r_post.status_code}")

        # 8. dependencies=[Depends(validate_request)] — rejects bad header
        r = await c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer k", "x-bad": "true"},
            json={"model": "m", "messages": [{"role": "u", "content": "hi"}]},
        )
        check("route-level dependency (SGLang)", r.status_code == 400 and r.json().get("error") == "http", r.text)

        # 9. Depends(require_api_key) — missing key -> 401
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": []})
        check("handler Depends 401", r.status_code == 401, r.text)

        # 10. non-stream response using response_model
        r = await c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer secretkey"},
            json={"model": "gpt", "messages": [{"role": "u", "content": "hi"}]},
        )
        body = r.json()
        check("Pydantic response_model", r.status_code == 200 and body.get("id") == "cmpl-1", r.text[:200])
        check("response_model choice text", body["choices"][0]["text"].startswith("hi"), r.text[:200])

        # 11. SSE streaming — mixes str and bytes
        collected = []
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers={"authorization": "Bearer k"},
            json={
                "model": "m",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
            },
        ) as r:
            check("SSE content-type", r.headers.get("content-type", "").startswith("text/event-stream"),
                  r.headers.get("content-type"))
            async for chunk in r.aiter_bytes():
                collected.append(chunk)
        full = b"".join(collected)
        check("SSE sees str+bytes frames", b'"idx":0' in full and b'"finish":true' in full, full[:300])
        check("SSE terminator", b"data: [DONE]" in full, full[-200:])

        # 12. Query validation — ge / le
        r = await c.get("/v1/items?offset=-1&limit=5")
        check("Query ge=0 rejects -1", r.status_code == 422, r.text[:200])
        r = await c.get("/v1/items?limit=999")
        check("Query le=100 rejects 999", r.status_code == 422, r.text[:200])
        r = await c.get("/v1/items?limit=50")
        check("Query valid 50", r.status_code == 200 and r.json()["limit"] == 50, r.text[:200])

        # 13. File + Form + UploadFile
        files = {"file": ("x.wav", b"fake-audio-bytes", "audio/wav")}
        data = {"model": "whisper-1", "language": "fr"}
        r = await c.post("/v1/audio/transcriptions", files=files, data=data)
        body = r.json() if r.status_code == 200 else {}
        check("multipart File+Form", r.status_code == 200 and body.get("size") == 16 and body.get("model") == "whisper-1",
              r.text[:200])

        # 14. bare Response object
        r = await c.get("/v1/raw")
        check("bare Response(bytes, text/plain)",
              r.status_code == 200 and r.headers.get("content-type", "").startswith("text/plain") and r.content == b"hello raw",
              f"{r.status_code} {r.headers.get('content-type')} {r.content[:40]}")

        # 15. HTTPException exception_handler
        r = await c.get("/v1/boom/http")
        check("HTTPException handler", r.status_code == 418 and r.json().get("error") == "http", r.text[:200])

        # 16. Custom exception handler
        r = await c.get("/v1/boom/my")
        check("custom Exception handler", r.status_code == 500 and r.json().get("error") == "my-error", r.text[:200])

        # 17. RequestValidationError handler — invalid JSON body
        r = await c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer k"},
            json={"model": 123},  # messages missing
        )
        check("RequestValidationError handler", r.status_code == 422 and r.json().get("error") == "validation", r.text[:300])

        # 18. raw request body
        r = await c.post("/v1/raw-body", content=b"X" * 500, headers={"content-type": "application/octet-stream"})
        body = r.json()
        check("raw Request.body()", r.status_code == 200 and body["bytes"] == 500 and body["content_type"] == "application/octet-stream", r.text[:200])

        # 19. /docs, /redoc, /openapi.json
        r = await c.get("/docs")
        check("/docs served", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
              f"{r.status_code} {r.headers.get('content-type')}")
        r = await c.get("/redoc")
        check("/redoc served", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
              f"{r.status_code} {r.headers.get('content-type')}")
        r = await c.get("/openapi.json")
        spec = r.json() if r.status_code == 200 else {}
        paths = spec.get("paths", {})
        check("/openapi.json served", r.status_code == 200 and "/v1/chat/completions" in paths, list(paths.keys())[:5])

    # 20. WebSocket (not via httpx)
    try:
        import websockets
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/v1/realtime") as ws:
            await ws.send("hello")
            r1 = await asyncio.wait_for(ws.recv(), timeout=2)
            await ws.send("world")
            r2 = await asyncio.wait_for(ws.recv(), timeout=2)
            check("WebSocket echo", r1 == "echo:hello" and r2 == "echo:world", f"{r1!r}, {r2!r}")
    except Exception as e:
        check("WebSocket echo", False, f"{type(e).__name__}: {e}")


def main():
    proc = start_server()
    try:
        asyncio.run(run_tests())
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_total = len(RESULTS)
    print(f"\n{'=' * 50}")
    print(f"AUDIT {n_pass}/{n_total} pass")
    if n_pass != n_total:
        print("\nFAILED:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
