"""Qwen-Agent / Qwen OpenAI API compatibility audit.

Exercises every FastAPI/Starlette pattern found in Qwen's codebase.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import List, Optional

import fastapi_turbo.compat  # noqa: install shims

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse


# ── Pydantic models (matching Qwen's openai_api.py) ──

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: Optional[int] = None
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage


class ChatCompletionResponse(BaseModel):
    id: str
    model: str
    choices: List[ChatChoice]


class ModelCard(BaseModel):
    id: str
    object: str = "model"


class ModelList(BaseModel):
    data: List[ModelCard]


# ── Custom middleware (Qwen's BasicAuthMiddleware pattern) ──

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request.headers.get("authorization", "")
        if request.url.path != "/health" and not token.startswith("Bearer "):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["x-auth-checked"] = "true"
        return response


# ── Lifespan (Qwen pattern) ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model_loaded = True
    yield


# ── App ──

app = FastAPI(title="Qwen Audit", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(AuthMiddleware)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/v1/models", response_model=ModelList)
async def list_models():
    return ModelList(data=[ModelCard(id="qwen-7b")])


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    if request.stream:
        # SSE streaming path (sse_starlette)
        from sse_starlette.sse import EventSourceResponse

        async def gen():
            for i in range(3):
                yield {"data": f'{{"idx":{i},"delta":"tok"}}'}
            yield {"data": "[DONE]"}

        return EventSourceResponse(gen())

    return ChatCompletionResponse(
        id="chatcmpl-1",
        model=request.model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content="hello"),
            )
        ],
    )


# Starlette direct imports test
@app.get("/starlette-test")
async def starlette_test(request: StarletteRequest):
    return StarletteResponse(content=b"starlette ok", media_type="text/plain")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19700
    app.run("127.0.0.1", port)
