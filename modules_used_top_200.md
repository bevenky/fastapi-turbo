# FastAPI Import Patterns: Top 200+ Repos Audit

## Summary

- **Repos analyzed**: 205+ across 5 research agents
- **Unique import paths tested**: 78
- **Coverage**: All top FastAPI projects (Open WebUI, LiteLLM, Airflow, vLLM, langchain-serve, etc.)

## Import Frequency by Tier

| Tier | Description | Count | fastapi-rs Status |
|------|-------------|-------|-------------------|
| 1 | Used by 20+ repos (must work) | 18 imports | 18/18 PASS |
| 2 | Used by 5-19 repos | 17 imports | 17/17 PASS |
| 3 | Used by 1-4 repos (edge/ecosystem) | 15 imports | 12/15 PASS |
| Starlette | Direct starlette.* imports | 20 imports | 19/20 PASS |
| SSE | Third-party SSE patterns | 2 imports | 2/2 PASS |
| Deep internals | Ecosystem plugin internals | 6 imports | N/A (stubs) |

## Tier 1: Universal (20+ repos)

These imports appear in essentially every FastAPI project.

| Import | Repos | Notes |
|--------|-------|-------|
| `from fastapi import FastAPI` | universal | App entry point |
| `from fastapi import HTTPException` | 25+ | Error handling |
| `from fastapi import Request` | 23+ | Raw request access |
| `from fastapi import Depends` | 22+ | Dependency injection |
| `from fastapi import APIRouter` | 20+ | Route organization |
| `from fastapi import status` | 18+ | HTTP status codes |
| `from fastapi.middleware.cors import CORSMiddleware` | 18+ | CORS setup |
| `from fastapi.responses import JSONResponse` | 17+ | Custom JSON responses |
| `from fastapi.responses import StreamingResponse` | 16+ | LLM streaming, file downloads |
| `from fastapi import Response` | 15+ | Generic response |
| `from fastapi import Body` | 15+ | Request body params |
| `from fastapi import UploadFile, File, Form` | 14+ | File upload handling |
| `from fastapi import Query` | 12+ | Query parameters |
| `from fastapi import BackgroundTasks` | 12+ | Async task scheduling |
| `from fastapi.responses import FileResponse` | 10+ | Static file serving |
| `from fastapi.responses import RedirectResponse` | 10+ | URL redirects |
| `from fastapi.exceptions import RequestValidationError` | 10+ | Custom validation errors |
| `from fastapi.security import OAuth2PasswordBearer` | 10+ | JWT auth pattern |

## Tier 2: Common (5-19 repos)

| Import | Repos | Notes |
|--------|-------|-------|
| `from fastapi import Path` | 11+ | Path parameters |
| `from fastapi import Header` | 8+ | Header extraction |
| `from fastapi import Cookie` | 5+ | Cookie extraction |
| `from fastapi import WebSocket, WebSocketDisconnect` | 8+ | WebSocket endpoints |
| `from fastapi import Security` | 6+ | Security dependencies |
| `from fastapi.responses import HTMLResponse` | 7+ | HTML endpoints |
| `from fastapi.responses import PlainTextResponse` | 3+ | Text responses |
| `from fastapi.responses import ORJSONResponse` | 5+ | Fast JSON via orjson |
| `from fastapi.staticfiles import StaticFiles` | 8+ | Static file mounting |
| `from fastapi.middleware.gzip import GZipMiddleware` | 7+ | Response compression |
| `from fastapi.encoders import jsonable_encoder` | 7+ | Model serialization |
| `from fastapi.routing import APIRoute` | 7+ | Custom route classes |
| `from fastapi.concurrency import run_in_threadpool` | 6+ | Sync-to-async bridge |
| `from fastapi.security import HTTPBearer` | 5+ | Bearer token auth |
| `from fastapi.security import APIKeyHeader` | 5+ | API key auth |
| `from fastapi.security import OAuth2PasswordRequestForm` | 5+ | Login forms |
| `from fastapi.security import SecurityScopes` | 3+ | OAuth2 scopes |
| `from fastapi.templating import Jinja2Templates` | 5+ | Server-side rendering |
| `from fastapi.openapi.utils import get_openapi` | 5+ | Custom OpenAPI schema |
| `from fastapi.openapi.docs import get_swagger_ui_html` | 3+ | Custom docs page |
| `from fastapi.security import HTTPBasicCredentials` | 3+ | Basic auth |
| `from fastapi.security import OAuth2AuthorizationCodeBearer` | 2+ | OAuth2 code flow |
| `from fastapi.datastructures import Default` | 2+ | Router sentinel |
| `from fastapi.utils import generate_unique_id` | 2+ | OpenAPI operation IDs |
| `from fastapi.types import DecoratedCallable` | 2+ | Type annotation |

## Tier 3: Edge Cases (1-4 repos)

| Import | Repos | Used by |
|--------|-------|---------|
| `from fastapi import applications` | 2 | Open WebUI, LiteLLM |
| `from fastapi import params` | 2 | Strawberry, fastapi_mcp |
| `from fastapi.routing import APIWebSocketRoute` | 1 | LiteLLM |
| `from fastapi.routing import Mount` | 1 | Airflow |
| `from fastapi.security.api_key import APIKeyHeader` | 2 | LiteLLM, Infinity |
| `from fastapi.security.oauth2 import OAuth2PasswordBearer` | 1 | Deep imports |
| `from fastapi.security.http import HTTPBearer` | 1 | Deep imports |
| `from fastapi.security.base import SecurityBase` | 2 | Azure-auth, Strawberry |
| `from fastapi.security.utils import get_authorization_scheme_param` | 1 | langcorn |
| `from fastapi.openapi.constants import REF_PREFIX` | 1 | OpenAPI tools |
| `from fastapi.middleware.wsgi import WSGIMiddleware` | 1 | Airflow |
| `from fastapi.dependencies.utils import get_dependant` | 1 | fastapi-jsonrpc |
| `import fastapi._compat` | 1 | fastapi-jsonrpc |
| `from fastapi.sse import EventSourceResponse, ServerSentEvent` | new | FastAPI 0.134+ |
| `from fastapi.responses import EventSourceResponse` | 2+ | LLM projects |

## Starlette Direct Imports

Many projects import from starlette directly, especially middleware and low-level types.

| Import | Repos | Notes |
|--------|-------|-------|
| `from starlette.middleware.cors import CORSMiddleware` | 10+ | Preferred by some |
| `from starlette.middleware.base import BaseHTTPMiddleware` | 8+ | Custom middleware pattern |
| `from starlette.responses import Response, StreamingResponse, JSONResponse` | 10+ | Response classes |
| `from starlette.requests import Request, HTTPConnection` | 7+ | Request types |
| `from starlette.middleware.sessions import SessionMiddleware` | 7+ | Session support |
| `from starlette.datastructures import State, URL, Headers, MutableHeaders` | 7+ | Data types |
| `from starlette.staticfiles import StaticFiles` | 5+ | File serving |
| `from starlette.exceptions import HTTPException` | 5+ | Exception base |
| `from starlette.types import ASGIApp, Receive, Scope, Send` | 5+ | ASGI types |
| `from starlette.websockets import WebSocket, WebSocketState, WebSocketDisconnect` | 5+ | WS types |
| `from starlette.routing import Mount, Route, BaseRoute, Match` | 5+ | Routing primitives |
| `from starlette.concurrency import run_in_threadpool, iterate_in_threadpool` | 4+ | Async helpers |
| `from starlette.background import BackgroundTask, BackgroundTasks` | 3+ | Background tasks |
| `from starlette.applications import Starlette` | 3+ | App base |
| `from starlette.middleware import Middleware` | 3+ | Middleware wrapper |
| `from starlette.datastructures import FormData, UploadFile, Secret` | 2+ | Form handling |
| `from starlette.formparsers import MultiPartParser` | 2+ | Multipart config |
| `from starlette.config import Config` | 1+ | Starlette config |
| `from starlette.routing import compile_path` | 1+ | Path regex compiler |
| `from starlette.templating import Jinja2Templates` | 2+ | SSR templates |

## Third-Party SSE (LLM Ecosystem)

| Import | Repos | Notes |
|--------|-------|-------|
| `from sse_starlette import EventSourceResponse` | 8+ | De facto standard for LLM streaming |
| `from sse_starlette.sse import EventSourceResponse, ServerSentEvent` | 3+ | Deep import variant |

## Notable Architectural Patterns

### 1. OAuth2 + JWT Auth (22+ repos)
```python
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
```

### 2. Custom Exception Handlers (10+ repos)
```python
from fastapi.exceptions import RequestValidationError
@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc): ...
```

### 3. LLM Streaming Response (16+ repos)
```python
from fastapi.responses import StreamingResponse
# or
from sse_starlette import EventSourceResponse
return StreamingResponse(generate(), media_type="text/event-stream")
```

### 4. BaseHTTPMiddleware Subclass (8+ repos)
```python
from starlette.middleware.base import BaseHTTPMiddleware
class CustomMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next): ...
```

### 5. Background Task Chaining (12+ repos)
```python
from fastapi import BackgroundTasks
@app.post("/")
async def endpoint(bg: BackgroundTasks):
    bg.add_task(send_email, ...)
```

### 6. Dependency Injection Chains (22+ repos)
```python
from fastapi import Depends
async def get_db(): ...
async def get_current_user(db=Depends(get_db)): ...
@app.get("/")
async def read(user=Depends(get_current_user)): ...
```

### 7. File Upload with Form Data (14+ repos)
```python
from fastapi import UploadFile, File, Form
@app.post("/upload")
async def upload(file: UploadFile = File(...), name: str = Form(...)): ...
```

### 8. Router Composition (20+ repos)
```python
from fastapi import APIRouter
router = APIRouter(prefix="/api/v1", tags=["users"])
app.include_router(router)
```

### 9. Custom OpenAPI Schema (5+ repos)
```python
from fastapi.openapi.utils import get_openapi
def custom_openapi():
    schema = get_openapi(title="My API", version="1.0", routes=app.routes)
    app.openapi_schema = schema
    return schema
```

### 10. WebSocket Manager Pattern (8+ repos)
```python
from fastapi import WebSocket, WebSocketDisconnect
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket): ...
```

## Deep Internal Imports (Ecosystem Plugins)

These are used by FastAPI extension libraries, not application code. They access private/internal APIs.

| Import | Used by | Risk |
|--------|---------|------|
| `fastapi.dependencies.utils.get_typed_signature` | fastapi-cache | Semi-stable |
| `fastapi.dependencies.utils.get_parameterless_sub_dependant` | fastapi-pagination | Semi-stable |
| `fastapi.dependencies.utils.get_body_field` | fastapi-pagination | Semi-stable |
| `fastapi.dependencies.utils._should_embed_body_fields` | fastapi-jsonrpc | Private, breaks often |
| `fastapi.routing.request_response` | fastapi-pagination | Semi-stable |
| `fastapi.routing.serialize_response` | fastapi-jsonrpc | Semi-stable |
| `fastapi.routing._merge_lifespan_context` | tortoise-orm | Private, breaks often |
| `fastapi._compat.ModelField` | fastapi-jsonrpc | Pydantic v1/v2 bridge |
