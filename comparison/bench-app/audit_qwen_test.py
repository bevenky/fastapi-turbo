"""Test runner for Qwen audit."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 19700
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def start_server():
    py = "/Users/venky/tech/fastapi_turbo_env/bin/python"
    log = open("/tmp/audit_qwen.log", "wb")
    proc = subprocess.Popen(
        [py, "audit_qwen.py", str(PORT)],
        cwd=str(Path(__file__).parent),
        stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/health", timeout=0.5)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.1)
        if proc.poll() is not None:
            break
    proc.kill()
    log.close()
    with open("/tmp/audit_qwen.log") as f:
        raise RuntimeError(f"server not up:\n{f.read()[-2000:]}")


async def run_tests():
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        # 1. Health (no auth required)
        r = await c.get("/health")
        check("health (no auth)", r.status_code == 200 and r.json().get("status") == "ok", r.text)

        # 2. Auth middleware rejects unauthenticated
        r = await c.get("/v1/models")
        check("auth middleware rejects", r.status_code == 401, f"{r.status_code}")

        # 3. Auth middleware passes with Bearer token
        r = await c.get("/v1/models", headers={"authorization": "Bearer test"})
        check("auth middleware passes", r.status_code == 200, r.text[:100])
        check("x-auth-checked header", r.headers.get("x-auth-checked") == "true", r.headers.get("x-auth-checked"))

        # 4. response_model=ModelList
        body = r.json()
        check("response_model ModelList", "data" in body and body["data"][0]["id"] == "qwen-7b", body)

        # 5. Non-stream chat completion
        r = await c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer k"},
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        body = r.json()
        check("chat completion non-stream",
              r.status_code == 200 and body.get("choices", [{}])[0].get("message", {}).get("content") == "hello",
              str(body)[:200])

        # 6. response_model ChatCompletionResponse shape
        check("response has id+model+choices",
              "id" in body and "model" in body and "choices" in body, list(body.keys()))

        # 7. SSE stream (sse_starlette EventSourceResponse)
        collected = []
        async with c.stream(
            "POST", "/v1/chat/completions",
            headers={"authorization": "Bearer k"},
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as r:
            check("SSE content-type", "text/event-stream" in r.headers.get("content-type", ""),
                  r.headers.get("content-type"))
            async for chunk in r.aiter_bytes():
                collected.append(chunk)
        full = b"".join(collected)
        check("SSE has data frames", b'"idx":0' in full, full[:300])
        check("SSE has DONE", b"[DONE]" in full, full[-200:])

        # 8. Pydantic Field validation (temperature ge=0, le=2)
        r = await c.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer k"},
            json={"model": "q", "messages": [{"role": "u", "content": "x"}], "temperature": 5.0},
        )
        check("Field(ge=0, le=2) rejects 5.0", r.status_code == 422, f"{r.status_code}")

        # 9. Starlette direct imports
        r = await c.get("/starlette-test", headers={"authorization": "Bearer k"})
        check("starlette Request+Response", r.status_code == 200 and r.content == b"starlette ok",
              f"{r.status_code} {r.content[:50]}")

        # 10. CORS headers present
        r = await c.get("/health")
        has_cors = "access-control-allow-origin" in {k.lower() for k in r.headers}
        check("CORS headers on response", has_cors, dict(r.headers))


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
    print(f"\n{'='*50}")
    print(f"QWEN AUDIT {n_pass}/{len(RESULTS)} pass")
    if n_pass != len(RESULTS):
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAIL: {name}: {detail[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
