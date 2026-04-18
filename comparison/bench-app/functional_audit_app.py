"""
Comprehensive Functional Audit App for fastapi-rs.
Tests 70 FastAPI behavior patterns end-to-end.
"""
import os, sys, enum, tempfile, pathlib
from contextlib import asynccontextmanager
from typing import Optional
from pydantic import BaseModel, Field

# Ensure fastapi_rs is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi_rs import (
    FastAPI, Depends, APIRouter, Request, Response,
    JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
    StreamingResponse, FileResponse, HTTPException, RequestValidationError,
    Query, Path, Header, Cookie, Body, Form, File, UploadFile,
    BackgroundTasks, status,
)
from fastapi_rs.security import OAuth2PasswordBearer, HTTPBearer, APIKeyHeader
from fastapi_rs.middleware.cors import CORSMiddleware
from fastapi_rs.middleware.base import BaseHTTPMiddleware

# ---- Pydantic Models ----

class SubItem(BaseModel):
    sub_name: str
    sub_value: int = 0

class ItemCreate(BaseModel):
    name: str
    price: float = Field(ge=0, description="Price must be non-negative")
    tags: list[str] = []
    description: Optional[str] = None
    sub_item: Optional[SubItem] = None

class ItemOut(BaseModel):
    name: str
    price: float
    in_stock: bool = True

class UserCreate(BaseModel):
    username: str
    email: str

class ItemColor(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"

# ---- Background task tracking ----
BG_TASK_LOG = []

def log_bg_task(message: str):
    BG_TASK_LOG.append(message)

# ---- Lifespan ----

@asynccontextmanager
async def lifespan(app):
    # startup
    app.state.audit_db = {"initialized": True}
    yield {"lifespan_data": "hello_from_lifespan"}
    # shutdown
    app.state.audit_db = None

app = FastAPI(
    title="Functional Audit",
    version="1.0.0",
    lifespan=lifespan,
)

# ---- CORS Middleware (test 49) ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://example.com"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ---- Custom HTTP middleware (test 48, 51) ----
@app.middleware("http")
async def add_timing_header(request, call_next):
    response = await call_next(request)
    if response is not None and hasattr(response, 'headers'):
        response.headers["x-audit-middleware"] = "applied"
    return response

# ---- Custom BaseHTTPMiddleware subclass (test 50) ----
class CustomBaseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response is not None and hasattr(response, 'headers'):
            response.headers["x-base-middleware"] = "yes"
        return response

app.add_middleware(CustomBaseMiddleware)

# ---- Exception handlers (tests 53-56) ----

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"custom_error": True, "detail": exc.detail},
        headers=exc.headers,
    )

@app.exception_handler(RequestValidationError)
async def custom_validation_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"custom_validation": True, "errors": exc.errors()},
    )

class MyCustomError(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)

@app.exception_handler(MyCustomError)
async def custom_error_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"custom_type": "MyCustomError", "msg": exc.msg},
    )

# ---- Security schemes (tests 59-61) ----
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
http_bearer = HTTPBearer()
api_key_header = APIKeyHeader(name="X-API-Key")

# ---- Dependencies (tests 22-29) ----

def simple_dep():
    return "simple_dep_value"

def sub_dep():
    return "sub_dep_value"

def chained_dep(sub=Depends(sub_dep)):
    return f"chained:{sub}"

def generator_dep():
    yield "gen_dep_value"
    # teardown code would go here

async def async_dep():
    return "async_dep_value"

class ClassDep:
    def __call__(self):
        return "class_dep_value"
class_dep_instance = ClassDep()

def route_level_dep():
    return "route_dep_ran"

def router_level_dep():
    return "router_dep_ran"

def original_dep():
    return "original"

# ---- ROUTING PATTERNS ----

# 1. Basic GET
@app.get("/test/basic-get")
def basic_get():
    return {"method": "GET", "ok": True}

# 2. POST with JSON body
@app.post("/test/basic-post")
def basic_post(item: ItemCreate):
    return {"name": item.name, "price": item.price}

# 3. PUT, PATCH, DELETE
@app.put("/test/put")
def test_put(item: ItemCreate):
    return {"method": "PUT", "name": item.name}

@app.patch("/test/patch")
def test_patch(item: ItemCreate):
    return {"method": "PATCH", "name": item.name}

@app.delete("/test/delete")
def test_delete():
    return {"method": "DELETE", "ok": True}

# 4. api_route with multiple methods
@app.api_route("/test/multi-method", methods=["GET", "POST"])
def multi_method():
    return {"multi": True}

# 5. Path parameters
@app.get("/test/items/{item_id}")
def get_item(item_id: str):
    return {"item_id": item_id}

# 6. Path with type (int)
@app.get("/test/typed-items/{item_id}")
def get_typed_item(item_id: int):
    return {"item_id": item_id, "type": "int"}

# 7. Query params
@app.get("/test/query")
def query_test(q: str = Query(...)):
    return {"q": q}

# 8. Query with validation
@app.get("/test/query-validated")
def query_validated(count: int = Query(ge=0, le=100)):
    return {"count": count}

# 9. Header params
@app.get("/test/header")
def header_test(x_token: str = Header(...)):
    return {"x_token": x_token}

# 10. Cookie params
@app.get("/test/cookie")
def cookie_test(session: str = Cookie(default="none")):
    return {"session": session}

# 11. Multiple body params with embed=True
@app.post("/test/multi-body")
def multi_body(item: ItemCreate = Body(embed=True), user: UserCreate = Body(embed=True)):
    return {"item_name": item.name, "username": user.username}

# 12. response_model
@app.get("/test/response-model", response_model=ItemOut)
def response_model_test():
    return {"name": "widget", "price": 9.99, "in_stock": True, "secret_field": "hidden"}

# 13. response_model_exclude_unset
@app.get("/test/response-model-exclude-unset", response_model=ItemOut, response_model_exclude_unset=True)
def response_model_exclude_unset_test():
    return ItemOut(name="widget", price=9.99)

# 14. status_code=201
@app.post("/test/status-201", status_code=201)
def status_201():
    return {"created": True}

# 15. tags
@app.get("/test/tagged", tags=["items"])
def tagged_test():
    return {"tagged": True}

# 16. summary and description
@app.get("/test/documented", summary="My Summary", description="My detailed description")
def documented_test():
    return {"documented": True}

# 17. deprecated
@app.get("/test/deprecated", deprecated=True)
def deprecated_test():
    return {"deprecated": True}

# 18. include_in_schema=False
@app.get("/test/hidden", include_in_schema=False)
def hidden_test():
    return {"hidden": True}

# ---- ROUTERS (tests 19-21) ----

inner_router = APIRouter(prefix="/inner", tags=["inner"])

@inner_router.get("/hello")
def inner_hello():
    return {"inner": True}

outer_router = APIRouter(prefix="/api/v1", tags=["v1"])

@outer_router.get("/ping")
def router_ping():
    return {"router": "v1", "pong": True}

# 21. Nested routers
outer_router.include_router(inner_router)

# 20. app.include_router
app.include_router(outer_router)

# Router with deps (test 28)
dep_router = APIRouter(prefix="/dep-router", tags=["dep-router"], dependencies=[Depends(router_level_dep)])

@dep_router.get("/info")
def dep_router_info():
    return {"dep_router": True}

app.include_router(dep_router)

# ---- DEPENDENCY INJECTION ----

# 22. Simple Depends
@app.get("/test/dep-simple")
def dep_simple(val=Depends(simple_dep)):
    return {"dep": val}

# 23. Chained depends
@app.get("/test/dep-chained")
def dep_chained(val=Depends(chained_dep)):
    return {"dep": val}

# 24. Generator dep (yield)
@app.get("/test/dep-generator")
def dep_generator(val=Depends(generator_dep)):
    return {"dep": val}

# 25. Async dep
@app.get("/test/dep-async")
async def dep_async(val=Depends(async_dep)):
    return {"dep": val}

# 26. Class-based dep
@app.get("/test/dep-class")
def dep_class(val=Depends(class_dep_instance)):
    return {"dep": val}

# 27. Route-level dependency (no return captured)
@app.get("/test/dep-route-level", dependencies=[Depends(route_level_dep)])
def dep_route_level():
    return {"route_level_dep": True}

# 29. dependency_overrides
@app.get("/test/dep-override")
def dep_override(val=Depends(original_dep)):
    return {"dep": val}

# ---- REQUEST/RESPONSE ----

# 30. Return dict -> auto JSON
@app.get("/test/return-dict")
def return_dict():
    return {"type": "dict", "value": 42}

# 31. Return Pydantic model -> auto JSON
@app.get("/test/return-model")
def return_model():
    return ItemOut(name="widget", price=5.0, in_stock=True)

# 32. JSONResponse
@app.get("/test/json-response")
def json_response():
    return JSONResponse(content={"custom": True}, status_code=200)

# 33. HTMLResponse
@app.get("/test/html-response")
def html_response():
    return HTMLResponse(content="<h1>Hello</h1>")

# 34. PlainTextResponse
@app.get("/test/plain-response")
def plain_response():
    return PlainTextResponse("hello plain")

# 35. RedirectResponse
@app.get("/test/redirect")
def redirect_response():
    return RedirectResponse(url="/test/basic-get")

# 36. StreamingResponse
@app.get("/test/streaming")
def streaming_response():
    def generate():
        for chunk in ["chunk1", "chunk2", "chunk3"]:
            yield chunk
    return StreamingResponse(generate(), media_type="text/plain")

# 37. FileResponse
# Create a temp file for FileResponse test
_tmp_dir = tempfile.mkdtemp()
_test_file = os.path.join(_tmp_dir, "test_audit.txt")
with open(_test_file, "w") as f:
    f.write("file content for audit test")

@app.get("/test/file-response")
def file_response():
    return FileResponse(_test_file)

# 38. set_cookie
@app.get("/test/set-cookie")
def set_cookie():
    resp = JSONResponse(content={"cookie_set": True})
    resp.set_cookie("audit_key", "audit_value")
    return resp

# 39. delete_cookie
@app.get("/test/delete-cookie")
def delete_cookie():
    resp = JSONResponse(content={"cookie_deleted": True})
    resp.delete_cookie("audit_key")
    return resp

# 40. Custom response headers
@app.get("/test/custom-headers")
def custom_headers(response: Response):
    response.headers["x-custom-header"] = "custom-value"
    return {"custom_header": True}

# ---- VALIDATION & MODELS ----

# 41. Pydantic body validation (reuses basic_post)

# 42. Pydantic model with Field (reuses ItemCreate with ge=0)

# 43. Optional fields
@app.post("/test/optional-fields")
def optional_fields(item: ItemCreate):
    return {"name": item.name, "description": item.description}

# 44. List fields (reuses ItemCreate.tags)

# 45. Nested models
@app.post("/test/nested-model")
def nested_model(item: ItemCreate):
    return {
        "name": item.name,
        "sub_item": item.sub_item.model_dump() if item.sub_item else None,
    }

# 46. response_model filters extra fields (reuses test 12)

# 47. Enum in query params
# NOTE: FastAPI auto-converts str->Enum, so handler can use color.value.
# fastapi-rs passes the raw str, so we test with str(color) which works either way.
@app.get("/test/enum-query")
def enum_query(color: ItemColor = Query(default=ItemColor.red)):
    # Handle both Enum objects and raw strings
    return {"color": color.value if hasattr(color, 'value') else color}

# 47b. Enum query WITHOUT workaround -- tests if .value works (FastAPI compat)
@app.get("/test/enum-query-strict")
def enum_query_strict(color: ItemColor = Query(default=ItemColor.red)):
    return {"color": color.value}

# ---- ERROR HANDLING ----

# 52. HTTPException
@app.get("/test/http-exception")
def http_exception_test():
    raise HTTPException(status_code=404, detail="item not found")

# 53. HTTPException with custom headers
@app.get("/test/http-exception-headers")
def http_exception_headers_test():
    raise HTTPException(status_code=403, detail="forbidden", headers={"X-Error": "yes"})

# 56. Custom exception
@app.get("/test/custom-exception")
def custom_exception_test():
    raise MyCustomError("something broke")

# ---- LIFECYCLE ----

# 57/58. Lifespan + app.state
@app.get("/test/lifespan-state")
def lifespan_state(request: Request):
    db = getattr(request.app.state, "audit_db", None)
    lifespan_data = getattr(request.app.state, "lifespan_data", None)
    return {"db_initialized": db is not None and db.get("initialized"), "lifespan_data": lifespan_data}

# ---- SECURITY ----

# 59. OAuth2PasswordBearer
@app.get("/test/security-oauth2")
def security_oauth2(token: str = Depends(oauth2_scheme)):
    return {"token": token}

# 60. HTTPBearer
@app.get("/test/security-bearer")
def security_bearer(creds=Depends(http_bearer)):
    return {"scheme": creds.scheme, "credentials": creds.credentials}

# 61. APIKeyHeader
@app.get("/test/security-apikey")
def security_apikey(key: str = Depends(api_key_header)):
    return {"api_key": key}

# ---- WEBSOCKET ----

# 62/63. WebSocket echo
@app.websocket("/test/ws-echo")
async def ws_echo(websocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"echo:{data}")
    except Exception:
        pass

# ---- SPECIAL ----

# 67. BackgroundTasks
@app.post("/test/background-task")
def background_task(bg: BackgroundTasks):
    bg.add_task(log_bg_task, "task_executed")
    return {"queued": True}

@app.get("/test/background-task-check")
def background_task_check():
    return {"log": list(BG_TASK_LOG)}

# 68. Form
@app.post("/test/form")
def form_test(username: str = Form(...), password: str = Form(...)):
    return {"username": username, "password": password}

# 69. File / UploadFile
@app.post("/test/upload")
async def upload_test(file: UploadFile = File(...)):
    contents = await file.read()
    return {"filename": file.filename, "size": len(contents)}

# 70. Request injection
@app.get("/test/request-injection")
def request_injection(request: Request):
    return {
        "method": request.method,
        "path": str(request.url),
        "has_headers": bool(request.headers),
    }

# ---- OPENAPI metadata check endpoints ----

# The /docs, /redoc, /openapi.json are served by the Rust core automatically.

if __name__ == "__main__":
    # Set the override BEFORE running (test 29)
    def mock_dep():
        return "overridden"
    app.dependency_overrides[original_dep] = mock_dep

    app.run("127.0.0.1", 19800)
