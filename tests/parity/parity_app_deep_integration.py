"""Deep integration parity app: multi-endpoint flows that mirror real apps.

Contains ~20-30 mini-apps mounted as APIRouters, each covering a cohesive
real-world workflow: auth, CRUD, pagination, file upload, SSE, error
chains, middleware, streaming, content negotiation, etc.

Uses ONLY stock FastAPI imports. The compat shim maps these to fastapi-rs
when running under the fastapi-rs process.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import uuid
from typing import Annotated, Any, Optional

from fastapi import (
    FastAPI,
    APIRouter,
    Depends,
    Query,
    Path,
    Header,
    Cookie,
    Body,
    Form,
    File,
    UploadFile,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import (
    JSONResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
    Response as FAResponse,
)
from fastapi.security import (
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    HTTPBearer,
    HTTPAuthorizationCredentials,
    APIKeyHeader,
    APIKeyQuery,
    APIKeyCookie,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, EmailStr


# ── App init + middleware stack ─────────────────────────────────────

app = FastAPI(
    title="Deep Integration Parity App",
    version="1.0.0",
    description="Multi-endpoint flows testing real-world FastAPI patterns.",
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],  # permissive so localhost and 127.0.0.1 both pass
)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Request-ID", "Link", "ETag"],
)


# =============================================================================
# MINI-APP 1: AUTH (JWT-ish using HMAC, Bearer token, OAuth2 password flow)
# =============================================================================

auth_router = APIRouter(prefix="/auth", tags=["auth"])

_SECRET = "parity-secret-do-not-use-in-prod"
_USERS = {
    "alice": {"username": "alice", "password": "wonderland", "role": "admin", "email": "alice@example.com"},
    "bob":   {"username": "bob",   "password": "builder",    "role": "user",  "email": "bob@example.com"},
    "eve":   {"username": "eve",   "password": "hunter2",    "role": "user",  "email": "eve@example.com"},
}


def _make_token(username: str, role: str) -> str:
    payload = f"{username}:{role}:{int(time.time())}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _verify_token(token: str) -> Optional[dict]:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.split(":")
        if len(parts) != 4:
            return None
        username, role, ts, sig = parts
        payload = f"{username}:{role}:{ts}"
        expected = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return {"username": username, "role": role, "ts": int(ts)}
    except Exception:
        return None


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def get_current_user(token: Annotated[Optional[str], Depends(oauth2_scheme)]):
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    data = _verify_token(token)
    if not data:
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = _USERS.get(data["username"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: Annotated[dict, Depends(get_current_user)]):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


class LoginBody(BaseModel):
    username: str
    password: str


@auth_router.post("/login")
def auth_login(body: LoginBody):
    user = _USERS.get(body.username)
    if not user or user["password"] != body.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = _make_token(user["username"], user["role"])
    return {"token": token, "token_type": "bearer", "role": user["role"]}


@auth_router.post("/token")
def auth_token(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    user = _USERS.get(form.username)
    if not user or user["password"] != form.password:
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    token = _make_token(user["username"], user["role"])
    return {"access_token": token, "token_type": "bearer"}


@auth_router.get("/me")
def auth_me(user: Annotated[dict, Depends(get_current_user)]):
    return {
        "username": user["username"],
        "role": user["role"],
        "email": user["email"],
    }


@auth_router.get("/protected")
def auth_protected(user: Annotated[dict, Depends(get_current_user)]):
    return {"ok": True, "user": user["username"]}


@auth_router.get("/admin")
def auth_admin(user: Annotated[dict, Depends(require_admin)]):
    return {"ok": True, "admin": user["username"]}


@auth_router.post("/refresh")
def auth_refresh(user: Annotated[dict, Depends(get_current_user)]):
    token = _make_token(user["username"], user["role"])
    return {"token": token, "token_type": "bearer"}


@auth_router.post("/logout")
def auth_logout(user: Annotated[dict, Depends(get_current_user)]):
    return {"ok": True, "message": "logged out"}


app.include_router(auth_router)


# =============================================================================
# MINI-APP 2: CRUD - items
# =============================================================================

crud_router = APIRouter(prefix="/items", tags=["items"])

_ITEMS: dict[int, dict] = {}
_ITEM_NEXT_ID = [1]


class ItemIn(BaseModel):
    name: str
    price: float
    tags: list[str] = Field(default_factory=list)


class ItemPatch(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    tags: Optional[list[str]] = None


class ItemOut(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


@crud_router.post("", status_code=201, response_model=ItemOut)
def items_create(body: ItemIn):
    # Reject duplicates by name
    for existing in _ITEMS.values():
        if existing["name"] == body.name:
            raise HTTPException(status_code=409, detail="Item already exists")
    iid = _ITEM_NEXT_ID[0]
    _ITEM_NEXT_ID[0] += 1
    item = {"id": iid, "name": body.name, "price": body.price, "tags": body.tags}
    _ITEMS[iid] = item
    return item


@crud_router.get("/{item_id}", response_model=ItemOut)
def items_read(item_id: int):
    item = _ITEMS.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@crud_router.put("/{item_id}", response_model=ItemOut)
def items_update(item_id: int, body: ItemIn):
    if item_id not in _ITEMS:
        raise HTTPException(status_code=404, detail="Item not found")
    item = {"id": item_id, "name": body.name, "price": body.price, "tags": body.tags}
    _ITEMS[item_id] = item
    return item


@crud_router.patch("/{item_id}", response_model=ItemOut)
def items_patch(item_id: int, body: ItemPatch):
    item = _ITEMS.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    update = body.model_dump(exclude_unset=True)
    item.update(update)
    _ITEMS[item_id] = item
    return item


@crud_router.delete("/{item_id}", status_code=204)
def items_delete(item_id: int):
    if item_id not in _ITEMS:
        raise HTTPException(status_code=404, detail="Item not found")
    del _ITEMS[item_id]
    return Response(status_code=204)


@crud_router.get("", response_model=list[ItemOut])
def items_list(
    response: Response,
    skip: int = 0,
    limit: int = 10,
):
    items_sorted = sorted(_ITEMS.values(), key=lambda i: i["id"])
    total = len(items_sorted)
    page = items_sorted[skip: skip + limit]
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Skip"] = str(skip)
    response.headers["X-Limit"] = str(limit)
    return page


app.include_router(crud_router)


# =============================================================================
# MINI-APP 3: Users CRUD with nested posts
# =============================================================================

users_router = APIRouter(prefix="/api/v1/users", tags=["users"])

_USERS_DB: dict[int, dict] = {}
_USER_NEXT_ID = [1]
_POSTS: dict[int, dict] = {}
_POST_NEXT_ID = [1]


class UserIn(BaseModel):
    name: str
    email: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str


class PostIn(BaseModel):
    title: str
    body: str


class PostOut(BaseModel):
    id: int
    user_id: int
    title: str
    body: str


@users_router.post("", status_code=201, response_model=UserOut)
def users_create(body: UserIn):
    uid = _USER_NEXT_ID[0]
    _USER_NEXT_ID[0] += 1
    user = {"id": uid, "name": body.name, "email": body.email}
    _USERS_DB[uid] = user
    return user


@users_router.get("/{user_id}", response_model=UserOut)
def users_read(user_id: int):
    u = _USERS_DB.get(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return u


@users_router.get("", response_model=list[UserOut])
def users_list(skip: int = 0, limit: int = 100):
    return sorted(_USERS_DB.values(), key=lambda u: u["id"])[skip: skip + limit]


@users_router.post("/{user_id}/posts", status_code=201, response_model=PostOut)
def posts_create(user_id: int, body: PostIn):
    if user_id not in _USERS_DB:
        raise HTTPException(status_code=404, detail="User not found")
    pid = _POST_NEXT_ID[0]
    _POST_NEXT_ID[0] += 1
    post = {"id": pid, "user_id": user_id, "title": body.title, "body": body.body}
    _POSTS[pid] = post
    return post


@users_router.get("/{user_id}/posts", response_model=list[PostOut])
def posts_list(user_id: int):
    if user_id not in _USERS_DB:
        raise HTTPException(status_code=404, detail="User not found")
    return [p for p in _POSTS.values() if p["user_id"] == user_id]


@users_router.get("/{user_id}/posts/{post_id}", response_model=PostOut)
def posts_read(user_id: int, post_id: int):
    if user_id not in _USERS_DB:
        raise HTTPException(status_code=404, detail="User not found")
    p = _POSTS.get(post_id)
    if not p or p["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Post not found")
    return p


app.include_router(users_router)


# =============================================================================
# MINI-APP 4: Pagination (X-Total-Count, Link headers, page-based, cursor)
# =============================================================================

pagination_router = APIRouter(prefix="/pagination", tags=["pagination"])

_DATASET = [{"id": i, "val": f"row-{i}"} for i in range(1, 101)]


@pagination_router.get("/skip-limit")
def pag_skip_limit(response: Response, skip: int = 0, limit: int = 10):
    page = _DATASET[skip: skip + limit]
    response.headers["X-Total-Count"] = str(len(_DATASET))
    links = []
    if skip + limit < len(_DATASET):
        links.append(f'</pagination/skip-limit?skip={skip+limit}&limit={limit}>; rel="next"')
    if skip > 0:
        prev_skip = max(0, skip - limit)
        links.append(f'</pagination/skip-limit?skip={prev_skip}&limit={limit}>; rel="prev"')
    if links:
        response.headers["Link"] = ", ".join(links)
    return page


@pagination_router.get("/page")
def pag_page(response: Response, page: int = 1, per_page: int = 10):
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    start = (page - 1) * per_page
    items = _DATASET[start: start + per_page]
    response.headers["X-Total-Count"] = str(len(_DATASET))
    response.headers["X-Page"] = str(page)
    response.headers["X-Per-Page"] = str(per_page)
    return {"page": page, "per_page": per_page, "total": len(_DATASET), "items": items}


@pagination_router.get("/cursor")
def pag_cursor(cursor: Optional[str] = None, limit: int = 10):
    start = 0
    if cursor:
        try:
            start = int(base64.urlsafe_b64decode(cursor.encode()).decode())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
    end = min(start + limit, len(_DATASET))
    items = _DATASET[start:end]
    next_cursor = None
    if end < len(_DATASET):
        next_cursor = base64.urlsafe_b64encode(str(end).encode()).decode()
    return {"items": items, "next_cursor": next_cursor}


app.include_router(pagination_router)


# =============================================================================
# MINI-APP 5: File upload
# =============================================================================

files_router = APIRouter(prefix="/files", tags=["files"])

_FILES: dict[str, dict] = {}
_MAX_FILE_SIZE = 1024 * 10  # 10 KB


@files_router.post("", status_code=201)
async def files_create(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large")
    if file.content_type and file.content_type.startswith("application/x-evil"):
        raise HTTPException(status_code=400, detail="Forbidden type")
    fid = uuid.uuid4().hex[:16]
    _FILES[fid] = {
        "id": fid,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content),
        "content": content,
    }
    return {"id": fid, "filename": file.filename, "size": len(content)}


@files_router.get("/{file_id}/meta")
def files_meta(file_id: str):
    f = _FILES.get(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return {"id": f["id"], "filename": f["filename"], "size": f["size"], "content_type": f["content_type"]}


@files_router.get("/{file_id}")
def files_read(file_id: str):
    f = _FILES.get(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=f["content"], media_type=f["content_type"] or "application/octet-stream")


@files_router.post("/multi", status_code=201)
async def files_multi(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        c = await f.read()
        out.append({"filename": f.filename, "size": len(c)})
    return {"count": len(out), "files": out}


@files_router.delete("/{file_id}", status_code=204)
def files_delete(file_id: str):
    if file_id not in _FILES:
        raise HTTPException(status_code=404, detail="File not found")
    del _FILES[file_id]
    return Response(status_code=204)


app.include_router(files_router)


# =============================================================================
# MINI-APP 6: SSE + background tasks
# =============================================================================

sse_router = APIRouter(prefix="/sse", tags=["sse"])


def _sse_gen(count: int):
    for i in range(count):
        yield f"data: event-{i}\n\n"


def _sse_typed_gen(count: int):
    for i in range(count):
        yield f"event: tick\nid: {i}\ndata: payload-{i}\n\n"


@sse_router.get("/events")
def sse_events(count: int = 3):
    return StreamingResponse(_sse_gen(count), media_type="text/event-stream")


@sse_router.get("/typed")
def sse_typed(count: int = 3):
    return StreamingResponse(_sse_typed_gen(count), media_type="text/event-stream")


@sse_router.get("/heartbeat")
def sse_heartbeat(count: int = 2):
    def gen():
        for i in range(count):
            yield f": keepalive {i}\n\n"
            yield f"data: tick-{i}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


app.include_router(sse_router)


# =============================================================================
# MINI-APP 7: NDJSON / streaming patterns
# =============================================================================

stream_router = APIRouter(prefix="/stream", tags=["stream"])


@stream_router.get("/ndjson")
def stream_ndjson(n: int = 5):
    def gen():
        for i in range(n):
            yield json.dumps({"i": i, "val": f"x-{i}"}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@stream_router.get("/plain")
def stream_plain(n: int = 5):
    def gen():
        for i in range(n):
            yield f"line-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


@stream_router.get("/bytes")
def stream_bytes(n: int = 5):
    def gen():
        for i in range(n):
            yield bytes([i])
    return StreamingResponse(gen(), media_type="application/octet-stream")


app.include_router(stream_router)


# =============================================================================
# MINI-APP 8: Error handling chain with custom exception handlers
# =============================================================================

errors_router = APIRouter(prefix="/errors", tags=["errors"])


class BusinessError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


class OtherBusinessError(Exception):
    def __init__(self, kind: str):
        self.kind = kind


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError):
    return JSONResponse(status_code=400, content={"error_code": exc.code, "error_message": exc.message})


@app.exception_handler(OtherBusinessError)
async def other_business_error_handler(request: Request, exc: OtherBusinessError):
    return JSONResponse(status_code=418, content={"kind": exc.kind})


@errors_router.get("/business")
def err_business():
    raise BusinessError(code="E_BUSINESS", message="business rule violated")


@errors_router.get("/other")
def err_other():
    raise OtherBusinessError(kind="teapot")


@errors_router.get("/http")
def err_http():
    raise HTTPException(status_code=402, detail="payment required")


@errors_router.get("/http-headers")
def err_http_headers():
    raise HTTPException(
        status_code=409,
        detail="conflict",
        headers={"X-Conflict-Reason": "duplicate"},
    )


@errors_router.get("/validation")
def err_validation(n: int):
    return {"n": n}


app.include_router(errors_router)


# =============================================================================
# MINI-APP 9: Content negotiation (Accept header, ETag, 304)
# =============================================================================

negot_router = APIRouter(prefix="/negot", tags=["negotiation"])


@negot_router.get("/content")
def negot_content(request: Request):
    accept = request.headers.get("accept", "application/json")
    if "text/html" in accept:
        return HTMLResponse("<html><body><h1>Hello</h1></body></html>")
    if "text/plain" in accept:
        return PlainTextResponse("Hello")
    return {"msg": "Hello"}


_ETAG_ENTRIES = {
    "doc1": {"etag": '"abc123"', "content": "content of doc1"},
    "doc2": {"etag": '"def456"', "content": "content of doc2"},
}


@negot_router.get("/etag/{doc_id}")
def negot_etag(doc_id: str, request: Request):
    doc = _ETAG_ENTRIES.get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Doc not found")
    inm = request.headers.get("if-none-match")
    if inm and inm == doc["etag"]:
        return Response(status_code=304, headers={"ETag": doc["etag"]})
    return Response(
        content=doc["content"],
        media_type="text/plain",
        headers={"ETag": doc["etag"]},
    )


app.include_router(negot_router)


# =============================================================================
# MINI-APP 10: Router-level dependencies and tag merging
# =============================================================================

def router_level_dep(x_tenant: Annotated[Optional[str], Header()] = None):
    if not x_tenant:
        raise HTTPException(status_code=400, detail="X-Tenant header required")
    return x_tenant


tenant_router = APIRouter(
    prefix="/tenant",
    tags=["tenant"],
    dependencies=[Depends(router_level_dep)],
)


@tenant_router.get("/info")
def tenant_info(x_tenant: Annotated[Optional[str], Header()] = None):
    return {"tenant": x_tenant}


@tenant_router.get("/stats", tags=["stats"])
def tenant_stats(x_tenant: Annotated[Optional[str], Header()] = None):
    return {"tenant": x_tenant, "stats": {"requests": 42}}


app.include_router(tenant_router)


# =============================================================================
# MINI-APP 11: Form vs JSON coexistence
# =============================================================================

ingress_router = APIRouter(prefix="/ingress", tags=["ingress"])


class Greeting(BaseModel):
    name: str
    shout: bool = False


@ingress_router.post("/json")
def ingress_json(body: Greeting):
    greeting = f"Hello, {body.name}"
    if body.shout:
        greeting = greeting.upper() + "!"
    return {"greeting": greeting, "source": "json"}


@ingress_router.post("/form")
def ingress_form(name: Annotated[str, Form()], shout: Annotated[bool, Form()] = False):
    greeting = f"Hello, {name}"
    if shout:
        greeting = greeting.upper() + "!"
    return {"greeting": greeting, "source": "form"}


app.include_router(ingress_router)


# =============================================================================
# MINI-APP 12: API-key schemes (header, query, cookie)
# =============================================================================

apikey_router = APIRouter(prefix="/apikey", tags=["apikey"])

_VALID_KEYS = {"KEY_HEADER_ABC", "KEY_QUERY_DEF", "KEY_COOKIE_GHI"}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)
api_key_cookie = APIKeyCookie(name="api_key", auto_error=False)


def require_api_key_header(k: Annotated[Optional[str], Depends(api_key_header)]):
    if not k or k not in _VALID_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return k


def require_api_key_query(k: Annotated[Optional[str], Depends(api_key_query)]):
    if not k or k not in _VALID_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return k


def require_api_key_cookie(k: Annotated[Optional[str], Depends(api_key_cookie)]):
    if not k or k not in _VALID_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return k


@apikey_router.get("/header")
def apikey_header(k: Annotated[str, Depends(require_api_key_header)]):
    return {"ok": True, "via": "header", "k": k}


@apikey_router.get("/query")
def apikey_query(k: Annotated[str, Depends(require_api_key_query)]):
    return {"ok": True, "via": "query", "k": k}


@apikey_router.get("/cookie")
def apikey_cookie(k: Annotated[str, Depends(require_api_key_cookie)]):
    return {"ok": True, "via": "cookie", "k": k}


app.include_router(apikey_router)


# =============================================================================
# MINI-APP 13: Cookies round-trip
# =============================================================================

cookie_router = APIRouter(prefix="/cookies", tags=["cookies"])


@cookie_router.post("/set")
def cookies_set(response: Response, value: str = "sess-123"):
    response.set_cookie("session_id", value, max_age=3600, httponly=True, path="/")
    return {"ok": True, "set": value}


@cookie_router.get("/read")
def cookies_read(session_id: Annotated[Optional[str], Cookie()] = None):
    return {"session_id": session_id}


@cookie_router.post("/clear")
def cookies_clear(response: Response):
    response.delete_cookie("session_id", path="/")
    return {"ok": True}


app.include_router(cookie_router)


# =============================================================================
# MINI-APP 14: Shopping cart (stateful flow)
# =============================================================================

cart_router = APIRouter(prefix="/cart", tags=["cart"])

_CARTS: dict[str, dict] = {}


@cart_router.post("", status_code=201)
def cart_create():
    cid = uuid.uuid4().hex[:8]
    _CARTS[cid] = {"id": cid, "items": [], "total": 0.0}
    return {"cart_id": cid}


@cart_router.get("/{cart_id}")
def cart_read(cart_id: str):
    c = _CARTS.get(cart_id)
    if not c:
        raise HTTPException(status_code=404, detail="Cart not found")
    return c


class CartItem(BaseModel):
    sku: str
    qty: int = 1
    price: float


@cart_router.post("/{cart_id}/items")
def cart_add(cart_id: str, item: CartItem):
    c = _CARTS.get(cart_id)
    if not c:
        raise HTTPException(status_code=404, detail="Cart not found")
    if item.qty < 1:
        raise HTTPException(status_code=400, detail="qty must be positive")
    c["items"].append({"sku": item.sku, "qty": item.qty, "price": item.price})
    c["total"] = round(sum(i["qty"] * i["price"] for i in c["items"]), 2)
    return c


@cart_router.delete("/{cart_id}/items/{sku}")
def cart_remove(cart_id: str, sku: str):
    c = _CARTS.get(cart_id)
    if not c:
        raise HTTPException(status_code=404, detail="Cart not found")
    before = len(c["items"])
    c["items"] = [i for i in c["items"] if i["sku"] != sku]
    if len(c["items"]) == before:
        raise HTTPException(status_code=404, detail="SKU not in cart")
    c["total"] = round(sum(i["qty"] * i["price"] for i in c["items"]), 2)
    return c


@cart_router.post("/{cart_id}/checkout")
def cart_checkout(cart_id: str):
    c = _CARTS.get(cart_id)
    if not c:
        raise HTTPException(status_code=404, detail="Cart not found")
    if not c["items"]:
        raise HTTPException(status_code=400, detail="Cart is empty")
    order_id = uuid.uuid4().hex[:12]
    order = {"order_id": order_id, "total": c["total"], "items": list(c["items"])}
    _CARTS[cart_id] = {"id": cart_id, "items": [], "total": 0.0}
    return order


app.include_router(cart_router)


# =============================================================================
# MINI-APP 15: Task queue (jobs with status)
# =============================================================================

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])

_JOBS: dict[str, dict] = {}


class JobIn(BaseModel):
    kind: str
    payload: dict = Field(default_factory=dict)


@jobs_router.post("", status_code=202)
def jobs_enqueue(body: JobIn):
    jid = uuid.uuid4().hex[:10]
    _JOBS[jid] = {"id": jid, "status": "queued", "kind": body.kind, "result": None}
    return {"job_id": jid, "status": "queued"}


@jobs_router.get("/{job_id}")
def jobs_status(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    return j


@jobs_router.post("/{job_id}/complete")
def jobs_complete(job_id: str, result: dict = Body(...)):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    j["status"] = "done"
    j["result"] = result
    return j


@jobs_router.post("/{job_id}/fail")
def jobs_fail(job_id: str, reason: str = Body(..., embed=True)):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    j["status"] = "failed"
    j["result"] = {"reason": reason}
    return j


app.include_router(jobs_router)


# =============================================================================
# MINI-APP 16: Redirect / status codes
# =============================================================================

redir_router = APIRouter(prefix="/redir", tags=["redir"])


@redir_router.get("/temp")
def redir_temp():
    return RedirectResponse(url="/auth/me", status_code=307)


@redir_router.get("/perm")
def redir_perm():
    return RedirectResponse(url="/auth/me", status_code=308)


@redir_router.get("/303")
def redir_303():
    return RedirectResponse(url="/auth/me", status_code=303)


app.include_router(redir_router)


# =============================================================================
# MINI-APP 17: Query & header & path parameter validation
# =============================================================================

val_router = APIRouter(prefix="/val", tags=["validation"])


@val_router.get("/q")
def val_q(n: Annotated[int, Query(ge=0, le=100)] = 5):
    return {"n": n}


@val_router.get("/q-list")
def val_q_list(tags: Annotated[list[str], Query()] = []):
    return {"tags": tags}


@val_router.get("/path/{name}")
def val_path(name: Annotated[str, Path(min_length=2, max_length=10)]):
    return {"name": name}


@val_router.get("/hdr")
def val_header(x_custom: Annotated[Optional[str], Header()] = None):
    return {"x_custom": x_custom}


@val_router.post("/body")
def val_body(x: Annotated[int, Body(embed=True, ge=0)]):
    return {"x": x}


app.include_router(val_router)


# =============================================================================
# MINI-APP 18: Search filter
# =============================================================================

search_router = APIRouter(prefix="/search", tags=["search"])

_SEARCH_DATA = [
    {"id": 1, "name": "apple", "category": "fruit", "price": 1.0},
    {"id": 2, "name": "banana", "category": "fruit", "price": 0.5},
    {"id": 3, "name": "carrot", "category": "veg", "price": 0.7},
    {"id": 4, "name": "doughnut", "category": "bakery", "price": 2.0},
    {"id": 5, "name": "eggplant", "category": "veg", "price": 1.5},
]


@search_router.get("")
def search(
    q: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: str = "id",
    order: str = "asc",
):
    results = list(_SEARCH_DATA)
    if q:
        results = [r for r in results if q.lower() in r["name"].lower()]
    if category:
        results = [r for r in results if r["category"] == category]
    if min_price is not None:
        results = [r for r in results if r["price"] >= min_price]
    if max_price is not None:
        results = [r for r in results if r["price"] <= max_price]
    if sort in {"id", "name", "price"}:
        results.sort(key=lambda r: r[sort], reverse=(order == "desc"))
    return {"count": len(results), "results": results}


app.include_router(search_router)


# =============================================================================
# MINI-APP 19: Comments with nested path params
# =============================================================================

posts_router = APIRouter(prefix="/posts", tags=["posts"])

_BLOG_POSTS: dict[int, dict] = {}
_BLOG_NEXT = [1]
_COMMENTS: dict[int, dict] = {}
_COMMENT_NEXT = [1]


class BlogIn(BaseModel):
    title: str
    body: str


@posts_router.post("", status_code=201)
def blog_create(b: BlogIn):
    pid = _BLOG_NEXT[0]
    _BLOG_NEXT[0] += 1
    p = {"id": pid, "title": b.title, "body": b.body}
    _BLOG_POSTS[pid] = p
    return p


@posts_router.get("/{post_id}")
def blog_read(post_id: int):
    p = _BLOG_POSTS.get(post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    return p


class CommentIn(BaseModel):
    author: str
    text: str


@posts_router.post("/{post_id}/comments", status_code=201)
def comment_create(post_id: int, c: CommentIn):
    if post_id not in _BLOG_POSTS:
        raise HTTPException(status_code=404, detail="Post not found")
    cid = _COMMENT_NEXT[0]
    _COMMENT_NEXT[0] += 1
    comment = {"id": cid, "post_id": post_id, "author": c.author, "text": c.text}
    _COMMENTS[cid] = comment
    return comment


@posts_router.get("/{post_id}/comments")
def comment_list(post_id: int):
    if post_id not in _BLOG_POSTS:
        raise HTTPException(status_code=404, detail="Post not found")
    return [c for c in _COMMENTS.values() if c["post_id"] == post_id]


@posts_router.delete("/{post_id}/comments/{comment_id}", status_code=204)
def comment_delete(post_id: int, comment_id: int):
    c = _COMMENTS.get(comment_id)
    if not c or c["post_id"] != post_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    del _COMMENTS[comment_id]
    return Response(status_code=204)


app.include_router(posts_router)


# =============================================================================
# MINI-APP 20: Rate-limiter simulation (header-based)
# =============================================================================

rl_router = APIRouter(prefix="/rl", tags=["ratelimit"])

_RL_COUNTERS: dict[str, int] = {}
_RL_LIMIT = 5


@rl_router.post("/hit")
def rl_hit(response: Response, x_client_id: Annotated[str, Header()] = "default"):
    count = _RL_COUNTERS.get(x_client_id, 0) + 1
    _RL_COUNTERS[x_client_id] = count
    remaining = max(0, _RL_LIMIT - count)
    response.headers["X-RateLimit-Limit"] = str(_RL_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if count > _RL_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": "60", "X-RateLimit-Limit": str(_RL_LIMIT), "X-RateLimit-Remaining": "0"},
        )
    return {"count": count, "limit": _RL_LIMIT}


@rl_router.post("/reset")
def rl_reset(x_client_id: Annotated[str, Header()] = "default"):
    _RL_COUNTERS.pop(x_client_id, None)
    return {"ok": True, "client_id": x_client_id}


app.include_router(rl_router)


# =============================================================================
# MINI-APP 21: Nested dependencies + context injection
# =============================================================================

ctx_router = APIRouter(prefix="/ctx", tags=["context"])


def get_db():
    return {"kind": "in_memory", "items": [1, 2, 3]}


def get_settings():
    return {"app_name": "parity", "debug": True}


def get_context(
    db: Annotated[dict, Depends(get_db)],
    settings: Annotated[dict, Depends(get_settings)],
):
    return {"db_kind": db["kind"], "app_name": settings["app_name"]}


@ctx_router.get("/info")
def ctx_info(ctx: Annotated[dict, Depends(get_context)]):
    return ctx


@ctx_router.get("/double")
def ctx_double(
    ctx1: Annotated[dict, Depends(get_context)],
    ctx2: Annotated[dict, Depends(get_context)],
):
    return {"same": ctx1 == ctx2, "ctx": ctx1}


app.include_router(ctx_router)


# =============================================================================
# MINI-APP 22: Healthcheck + version (top-level)
# =============================================================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    return {"ready": True}


@app.get("/version")
def version():
    return {"version": "1.0.0"}


@app.get("/")
def root():
    return {"hello": "world", "app": "deep-integration-parity"}


# =============================================================================
# MINI-APP 23: Path / operation metadata on APIRoute
# =============================================================================

meta_router = APIRouter(prefix="/meta", tags=["meta"])


@meta_router.get("/one", summary="First meta endpoint", description="Long desc")
def meta_one():
    return {"one": True}


@meta_router.get(
    "/two",
    summary="Second meta endpoint",
    description="Another long desc",
    response_description="Returns two thing",
    status_code=201,
    tags=["extra"],
)
def meta_two():
    return {"two": True}


@meta_router.get("/deprecated", deprecated=True)
def meta_deprecated():
    return {"deprecated": True}


app.include_router(meta_router)


# =============================================================================
# MINI-APP 24: Echo (for header/body round-trip)
# =============================================================================

echo_router = APIRouter(prefix="/echo", tags=["echo"])


@echo_router.get("/headers")
def echo_headers(request: Request):
    # Only include lower-cased well-known names to avoid server-specific extras
    wanted = {"user-agent", "accept", "x-echo", "content-type", "authorization"}
    result = {k.lower(): v for k, v in request.headers.items() if k.lower() in wanted}
    return result


@echo_router.post("/body")
async def echo_body(request: Request):
    body = await request.body()
    return {"len": len(body), "sha256": hashlib.sha256(body).hexdigest()}


@echo_router.get("/query")
def echo_query(request: Request):
    return dict(request.query_params)


app.include_router(echo_router)


# =============================================================================
# MINI-APP 25: Counter (idempotency flow)
# =============================================================================

ctr_router = APIRouter(prefix="/counter", tags=["counter"])

_COUNTERS: dict[str, int] = {}
_IDEMP: dict[str, dict] = {}


class IncBody(BaseModel):
    by: int = 1


@ctr_router.post("/{name}/inc")
def ctr_inc(name: str, body: IncBody, idempotency_key: Annotated[Optional[str], Header()] = None):
    if idempotency_key and idempotency_key in _IDEMP:
        return _IDEMP[idempotency_key]
    _COUNTERS[name] = _COUNTERS.get(name, 0) + body.by
    out = {"name": name, "value": _COUNTERS[name]}
    if idempotency_key:
        _IDEMP[idempotency_key] = out
    return out


@ctr_router.get("/{name}")
def ctr_get(name: str):
    return {"name": name, "value": _COUNTERS.get(name, 0)}


@ctr_router.delete("/{name}", status_code=204)
def ctr_del(name: str):
    _COUNTERS.pop(name, None)
    return Response(status_code=204)


app.include_router(ctr_router)


# =============================================================================
# MINI-APP 26: Top-level convenience so clients never get a blank 404.
# =============================================================================

@app.get("/routes")
def list_routes():
    rs = []
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = sorted(list(getattr(r, "methods", []) or []))
        if path:
            rs.append({"path": path, "methods": methods})
    return {"count": len(rs), "routes": rs}
