"""vLLM / SGLang feature-compatibility audit for fastapi-turbo.

Every pattern below was found in the vLLM or SGLang source tree
(see research notes). The audit runs this app on fastapi-turbo and
exercises each endpoint. Tests pass only if behaviour matches
FastAPI/Starlette semantics.

Covers:
  - FastAPI(lifespan=, openapi_url=, docs_url=, redoc_url=, root_path=)
  - app.state.<x>, request.app.state.<x>
  - APIRouter + include_router + @router.api_route(methods=[...])
  - Depends + dependencies=[Depends(...)] on routes
  - StreamingResponse with async generator (text/event-stream, mixing str+bytes)
  - ORJSONResponse, JSONResponse, Response
  - response_class= on routes (and default_response_class= on app)
  - HTTPException + RequestValidationError + custom Exception handlers
  - status.* constants via http.HTTPStatus (SGLang) AND fastapi.status (vLLM)
  - Pydantic v2 models in request body + response_model
  - Form / File / UploadFile
  - Query(default, ge=, le=)
  - CORSMiddleware + app.middleware("http") + add_middleware(Class, **kwargs)
  - WebSocket route (vLLM realtime)
  - /docs, /redoc, /openapi.json
"""
from __future__ import annotations

import http
import sys
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator

import fastapi_turbo.compat  # install sys.modules shims first
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    ORJSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel


# 1. Lifespan + app.state
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_ran = True
    app.state.counter = 0
    yield
    app.state.shutdown_ran = True


app = FastAPI(
    title="vLLM+SGLang Audit",
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    default_response_class=ORJSONResponse,  # SGLang sets this
)

# root_path (vLLM sets this post-construction)
app.root_path = "/api"


# 2. CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 3. Custom HTTP middleware (vLLM pattern for access logging)
@app.middleware("http")
async def log_and_id(request: Request, call_next):
    response = await call_next(request)
    response.headers["x-audit-middleware"] = "ok"
    return response


# 4. Exception handlers (both vLLM & SGLang register all three)
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return ORJSONResponse(
        {"error": "http", "detail": exc.detail}, status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
async def validation_exc_handler(request: Request, exc: RequestValidationError):
    return ORJSONResponse(
        {"error": "validation", "detail": str(exc.errors()[:1])}, status_code=422
    )


class MyError(Exception):
    pass


@app.exception_handler(MyError)
async def custom_exc_handler(request: Request, exc: MyError):
    return ORJSONResponse({"error": "my-error", "msg": str(exc)}, status_code=500)


# 5. Pydantic request + response models
class ChatMsg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMsg]
    temperature: float = 1.0
    max_tokens: int | None = None
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    text: str


class ChatResponse(BaseModel):
    id: str
    model: str
    choices: list[ChatChoice]


# 6. Dependencies — request-level + route-level
async def validate_request(request: Request) -> None:
    """Route-level dependency (SGLang pattern: dependencies=[Depends(...)])."""
    if request.headers.get("x-bad") == "true":
        raise HTTPException(status_code=400, detail="bad header")


def require_api_key(request: Request) -> str:
    """Handler-injected dependency (Depends(...))."""
    key = request.headers.get("authorization", "")
    if not key.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing key"
        )
    return key[7:]


# 7. Router + include_router + api_route
v1 = APIRouter(prefix="/v1")


@v1.api_route("/health", methods=["GET", "POST"])
async def health(request: Request):
    # hit app.state from request
    request.app.state.counter += 1
    return {"ok": True, "ran": request.app.state.startup_ran}


# 8. SSE streaming (text/event-stream) — mixes str and bytes yields
async def sse_chat_stream(messages: list[ChatMsg]) -> AsyncGenerator[str | bytes, None]:
    # vLLM pattern: yield str with f"data: {json}\n\n"
    for i, msg in enumerate(messages):
        yield f'data: {{"idx":{i},"role":"{msg.role}"}}\n\n'
    # SGLang pattern: yield bytes
    yield b'data: {"finish":true}\n\n'
    # terminator
    yield "data: [DONE]\n\n"


@v1.post(
    "/chat/completions",
    response_model=ChatResponse,
    dependencies=[Depends(validate_request)],  # SGLang route-level
)
async def chat_completions(
    body: ChatRequest,
    key: Annotated[str, Depends(require_api_key)],  # vLLM handler-level
):
    if body.stream:
        return StreamingResponse(
            sse_chat_stream(body.messages),
            media_type="text/event-stream",
        )
    # non-stream: use response_model
    return ChatResponse(
        id="cmpl-1",
        model=body.model,
        choices=[ChatChoice(index=0, text=f"hi {key[:4]}")],
    )


# 9. Query validation (SGLang: Query(0.0, ge=0.0))
@v1.get("/items")
async def list_items(
    offset: Annotated[int, Query(0, ge=0)],
    limit: Annotated[int, Query(10, ge=1, le=100)],
):
    return {"offset": offset, "limit": limit, "items": []}


# 10. Form + File + UploadFile (SGLang audio/transcriptions pattern)
@v1.post("/audio/transcriptions")
async def transcribe(
    file: Annotated[UploadFile, File(...)],
    model: Annotated[str, Form(...)],
    language: Annotated[str, Form(default="en")],
):
    data = await file.read()
    return {
        "filename": file.filename,
        "size": len(data),
        "model": model,
        "language": language,
    }


# 11. Manual Response object (SGLang uses bare Response)
@v1.get("/raw")
async def raw_response():
    return Response(
        content=b"hello raw", media_type="text/plain", status_code=200
    )


# 12. Custom error path (tests exception_handler dispatch)
@v1.get("/boom/http")
async def boom_http():
    raise HTTPException(status_code=418, detail="teapot")


@v1.get("/boom/my")
async def boom_my():
    raise MyError("blew up")


# 13. WebSocket (vLLM /v1/realtime pattern)
@v1.websocket("/realtime")
async def realtime(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "close":
                await ws.close()
                return
            await ws.send_text(f"echo:{msg}")
    except Exception:
        return


# 14. Raw Request body (vLLM parses bodies manually)
@v1.post("/raw-body")
async def raw_body(request: Request):
    b = await request.body()
    return {"bytes": len(b), "content_type": request.headers.get("content-type")}


app.include_router(v1)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19300
    app.run("127.0.0.1", port)
