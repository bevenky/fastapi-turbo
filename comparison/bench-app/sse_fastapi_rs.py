"""fastapi-rs SSE streaming bench — mirrors vLLM / SGLang pattern.

Every request streams N chunks followed by `data: [DONE]\n\n`. This matches
exactly how vLLM's `chat/completions` stream works: StreamingResponse with
media_type `text/event-stream`, async generator yielding `data: ...\n\n`
(mixing str + bytes yields is also supported).
"""
from __future__ import annotations

import asyncio
import os
import sys

import fastapi_rs.compat  # noqa
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse

app = FastAPI()


async def gen_tokens(n: int):
    # Simulate token stream — each chunk is a tiny data: line.
    for i in range(n):
        yield f'data: {{"idx":{i},"delta":"tok"}}\n\n'
    yield "data: [DONE]\n\n"


@app.get("/stream")
async def stream(n: int = Query(32, ge=1, le=4096)):
    return StreamingResponse(gen_tokens(n), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19500
    app.run("127.0.0.1", port)
