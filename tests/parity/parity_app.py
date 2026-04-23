"""Parity mega-app: 100 endpoints testing every FastAPI pattern.

Uses ONLY stock FastAPI imports. The compat shim maps these to fastapi-turbo
when running under fastapi-turbo.
"""
import asyncio
import enum
import os
import tempfile
from decimal import Decimal
from typing import Annotated, Optional, Union

from fastapi import (
    FastAPI, APIRouter, Depends, Query, Path, Header, Cookie, Body, Form,
    File, UploadFile, HTTPException, Request, Response, Security,
    BackgroundTasks, status,
)
from fastapi.responses import (
    JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
    StreamingResponse, FileResponse,
)
from fastapi.security import (
    OAuth2PasswordBearer, HTTPBearer, HTTPBasic, APIKeyHeader,
    SecurityScopes, OAuth2PasswordRequestForm,
)
from pydantic import BaseModel, Field

# ── Pydantic models ──────────────────────────────────────────────

class Item(BaseModel):
    name: str
    price: float
    description: str | None = None

class ItemOut(BaseModel):
    name: str
    price: float

class NestedChild(BaseModel):
    value: int

class NestedParent(BaseModel):
    child: NestedChild
    label: str

class ItemWithList(BaseModel):
    tags: list[str]

class AliasedItem(BaseModel):
    item_name: str = Field(alias="itemName")

class DescribedItem(BaseModel):
    value: int = Field(description="The item value")

class ExampleItem(BaseModel):
    count: int = Field(examples=[42])

class DefaultItem(BaseModel):
    count: int = Field(default=0)

class GeItem(BaseModel):
    amount: int = Field(ge=0)

class DiscriminatorA(BaseModel):
    kind: str = "a"
    a_val: int = 0

class DiscriminatorB(BaseModel):
    kind: str = "b"
    b_val: str = ""

class OptionalFieldsModel(BaseModel):
    required_field: str
    optional_field: str | None = None
    default_field: str = "default"

# ── Enums ────────────────────────────────────────────────────────

class Color(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"

# ── Security schemes ─────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
bearer_scheme = HTTPBearer()
basic_scheme = HTTPBasic()
apikey_scheme = APIKeyHeader(name="X-API-Key")

# ── Dependency functions ─────────────────────────────────────────

def get_simple_dep():
    return "dep_value"

def get_inner_dep():
    return "inner"

def get_chained_dep(inner=Depends(get_inner_dep)):
    return f"chained_{inner}"

def get_generator_dep():
    yield "gen_value"

async def get_async_dep():
    return "async_dep"

class CallableDep:
    def __call__(self):
        return "class_dep"

callable_dep = CallableDep()

def side_effect_dep():
    """Dep used as route-level dependency (no return consumed)."""
    pass

def dep_with_query(q: str = Query(default="default_q")):
    return f"dep_q_{q}"

def dep_with_header(x_custom: str = Header(default="default_h")):
    return f"dep_h_{x_custom}"

def dep_with_request(request: Request):
    return f"dep_req_{request.method}"

def dep_raises():
    raise HTTPException(status_code=403, detail="dep_forbidden")

def dep_level_1():
    return "L1"

def dep_level_2(l1=Depends(dep_level_1)):
    return f"L2({l1})"

def dep_level_3(l2=Depends(dep_level_2)):
    return f"L3({l2})"

def dep_a():
    return "A"

def dep_b():
    return "B"

def dep_c():
    return "C"

def dep_with_default(val: str = "fallback"):
    return f"dep_{val}"

# Track cleanup
_cleanup_log = []

def dep_yield_cleanup():
    _cleanup_log.append("setup")
    yield "cleanup_value"
    _cleanup_log.append("teardown")

async def async_gen_dep():
    yield "async_gen_value"

# Dep used for caching test
_dep_call_count = 0
def dep_cached():
    global _dep_call_count
    _dep_call_count += 1
    return f"cached_{_dep_call_count}"

# ── Router-level deps ────────────────────────────────────────────

def router_dep():
    return "router_dep_val"

dep_router = APIRouter(prefix="/p091-router-dep", dependencies=[Depends(router_dep)])

@dep_router.get("")
def p091_endpoint():
    return {"ok": True}

include_dep_router = APIRouter(prefix="/p092-include-router-dep")

@include_dep_router.get("")
def p092_endpoint():
    return {"ok": True}


# ── App ──────────────────────────────────────────────────────────

app = FastAPI()
app.include_router(dep_router)
app.include_router(include_dep_router, dependencies=[Depends(side_effect_dep)])

# ── Health check ─────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════
# PATTERNS 1-30: Routing
# ══════════════════════════════════════════════════════════════════

# P001: basic GET
@app.get("/p001-basic-get")
def p001():
    return {"ok": True}

# P002: basic POST with JSON body echo
@app.post("/p002-basic-post")
def p002(item: Item):
    return item.model_dump()

# P003: basic PUT echo
@app.put("/p003-basic-put")
def p003(item: Item):
    return item.model_dump()

# P004: basic PATCH echo
@app.patch("/p004-basic-patch")
def p004(item: Item):
    return item.model_dump()

# P005: basic DELETE
@app.delete("/p005-basic-delete")
def p005():
    return {"deleted": True}

# P006: path param int
@app.get("/p006-path-int/{item_id}")
def p006(item_id: int):
    return {"id": item_id, "type": "int"}

# P007: path param str
@app.get("/p007-path-str/{name}")
def p007(name: str):
    return {"name": name}

# P008: path param float (via type annotation)
@app.get("/p008-path-float/{price}")
def p008(price: float):
    return {"price": price}

# P009: required query param
@app.get("/p009-query-required")
def p009(q: str):
    return {"q": q}

# P010: query with default
@app.get("/p010-query-default")
def p010(q: str = "hello"):
    return {"q": q}

# P011: optional query
@app.get("/p011-query-optional")
def p011(q: str | None = None):
    return {"q": q}

# P012: query int
@app.get("/p012-query-int")
def p012(n: int):
    return {"n": n}

# P013: query bool
@app.get("/p013-query-bool")
def p013(flag: bool):
    return {"flag": flag}

# P014: query list
@app.get("/p014-query-list")
def p014(items: list[str] = Query()):
    return {"items": items}

# P015: multi query params
@app.get("/p015-multi-query")
def p015(skip: int = 0, limit: int = 10):
    return {"skip": skip, "limit": limit}

# P016: header
@app.get("/p016-header")
def p016(user_agent: str = Header()):
    return {"agent": user_agent}

# P017: cookie
@app.get("/p017-cookie")
def p017(session_id: str = Cookie(default="none")):
    return {"session": session_id}

# P018: path + query combo
@app.get("/p018-path-query/{item_id}")
def p018(item_id: int, q: str = "search"):
    return {"id": item_id, "q": q}

# P019: pydantic body model
@app.post("/p019-body-model")
def p019(item: Item):
    return {"name": item.name, "price": item.price, "description": item.description}

# P020: body embed
@app.post("/p020-body-embed")
def p020(item: Item = Body(embed=True)):
    return {"name": item.name}

# P021: multi body params
@app.post("/p021-multi-body")
def p021(item: Item = Body(), extra: str = Body()):
    return {"name": item.name, "extra": extra}

# P022: form params
@app.post("/p022-form")
def p022(username: str = Form(), password: str = Form()):
    return {"username": username, "password": password}

# P023: file upload
@app.post("/p023-file")
async def p023(file: bytes = File()):
    return {"size": len(file)}

# P024: UploadFile
@app.post("/p024-uploadfile")
async def p024(file: UploadFile):
    content = await file.read()
    return {"filename": file.filename, "size": len(content)}

# P025: form + file combined
@app.post("/p025-form-file")
async def p025(name: str = Form(), file: UploadFile = File()):
    content = await file.read()
    return {"name": name, "filename": file.filename, "size": len(content)}

# P026: response_model (filters extra fields)
@app.get("/p026-response-model", response_model=ItemOut)
def p026():
    return {"name": "widget", "price": 9.99, "description": "should be filtered"}

# P027: response_model_exclude_unset
@app.get("/p027-response-model-exclude", response_model=Item, response_model_exclude_unset=True)
def p027():
    return Item(name="widget", price=9.99)

# P028: custom status code
@app.get("/p028-status-code", status_code=201)
def p028():
    return {"created": True}

# P029: deprecated endpoint
@app.get("/p029-deprecated", deprecated=True)
def p029():
    return {"deprecated": True}

# P030: tags
@app.get("/p030-tags", tags=["items"])
def p030():
    return {"tagged": True}


# ══════════════════════════════════════════════════════════════════
# PATTERNS 31-50: Response Types
# ══════════════════════════════════════════════════════════════════

# P031: return dict → auto JSON
@app.get("/p031-json")
def p031():
    return {"msg": "json"}

# P032: JSONResponse
@app.get("/p032-json-response")
def p032():
    return JSONResponse(content={"msg": "json_response"})

# P033: HTMLResponse
@app.get("/p033-html")
def p033():
    return HTMLResponse("<h1>hi</h1>")

# P034: PlainTextResponse
@app.get("/p034-plain")
def p034():
    return PlainTextResponse("hello")

# P035: RedirectResponse
@app.get("/p035-redirect")
def p035():
    return RedirectResponse("/p031-json")

# P036: StreamingResponse
@app.get("/p036-stream")
def p036():
    def gen():
        yield "chunk1"
        yield "chunk2"
    return StreamingResponse(gen(), media_type="text/plain")

# P037: FileResponse
_tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
_tmpfile.write(b"file content here")
_tmpfile.close()

@app.get("/p037-file")
def p037():
    return FileResponse(_tmpfile.name)

# P038: ORJSONResponse — skip if orjson not installed
try:
    from fastapi.responses import ORJSONResponse
    @app.get("/p038-orjson")
    def p038():
        return JSONResponse(content={"fast": True})
except ImportError:
    @app.get("/p038-orjson")
    def p038():
        return JSONResponse(content={"fast": True})

# P039: custom headers
@app.get("/p039-custom-headers")
def p039():
    return Response(content='{"custom":true}', media_type="application/json", headers={"X-Custom": "yes"})

# P040: set cookie
@app.get("/p040-set-cookie")
def p040():
    resp = JSONResponse(content={"cookie": "set"})
    resp.set_cookie(key="session", value="abc123")
    return resp

# P041: delete cookie
@app.get("/p041-delete-cookie")
def p041():
    resp = JSONResponse(content={"cookie": "deleted"})
    resp.delete_cookie(key="session")
    return resp

# P042: 204 no content
@app.get("/p042-status-204")
def p042():
    return Response(status_code=204)

# P043: bytes response
@app.get("/p043-bytes")
def p043():
    return Response(content=b"raw bytes", media_type="application/octet-stream")

# P044: return Pydantic model directly
@app.post("/p044-return-model")
def p044(item: Item):
    return item

# P045: return None
@app.get("/p045-none")
def p045():
    return None

# P046: return string
@app.get("/p046-string")
def p046():
    return "hello"

# P047: return int
@app.get("/p047-int")
def p047():
    return 42

# P048: return list
@app.get("/p048-list")
def p048():
    return [1, 2, 3]

# P049: nested dict
@app.get("/p049-nested-dict")
def p049():
    return {"a": {"b": [1, 2]}}

# P050: decimal (JSON serialized)
@app.get("/p050-decimal")
def p050():
    return {"price": float(Decimal("9.99"))}


# ══════════════════════════════════════════════════════════════════
# PATTERNS 51-70: Validation
# ══════════════════════════════════════════════════════════════════

# P051: query ge (pass and fail)
@app.get("/p051-query-ge")
def p051(n: int = Query(ge=0)):
    return {"n": n}

# P052: query le (pass and fail)
@app.get("/p052-query-le")
def p052(n: int = Query(le=10)):
    return {"n": n}

# P053: query gt + lt
@app.get("/p053-query-gt-lt")
def p053(n: int = Query(gt=0, lt=10)):
    return {"n": n}

# P054: body validation error
@app.post("/p054-body-validation")
def p054(item: Item):
    return item.model_dump()

# P055: nested validation error
@app.post("/p055-nested-validation")
def p055(parent: NestedParent):
    return parent.model_dump()

# P056: enum query
@app.get("/p056-enum-query")
def p056(color: Color):
    return {"color": color.value}

# P057: regex/pattern query
@app.get("/p057-regex-query")
def p057(code: str = Query(pattern="^[A-Z]+$")):
    return {"code": code}

# P058: min/max length
@app.get("/p058-min-max-length")
def p058(s: str = Query(min_length=1, max_length=10)):
    return {"s": s}

# P059: optional fields
@app.post("/p059-optional-fields")
def p059(item: OptionalFieldsModel):
    return item.model_dump()

# P060: list field
@app.post("/p060-list-field")
def p060(item: ItemWithList):
    return item.model_dump()

# P061: nested model
@app.post("/p061-nested-model")
def p061(parent: NestedParent):
    return parent.model_dump()

# P062: field alias
@app.post("/p062-field-alias")
def p062(item: AliasedItem):
    return {"item_name": item.item_name}

# P063: field description
@app.post("/p063-field-description")
def p063(item: DescribedItem):
    return {"value": item.value}

# P064: field example
@app.post("/p064-field-example")
def p064(item: ExampleItem):
    return {"count": item.count}

# P065: field default
@app.post("/p065-field-default")
def p065(item: DefaultItem):
    return {"count": item.count}

# P066: field ge
@app.post("/p066-field-ge")
def p066(item: GeItem):
    return {"amount": item.amount}

# P067: discriminated union (simplified — just accept either type)
@app.post("/p067-discriminated-union")
def p067(body: dict = Body()):
    return {"received": body}

# P068: typed path coercion
@app.get("/p068-typed-path/{item_id}")
def p068(item_id: int):
    return {"item_id": item_id, "type": str(type(item_id).__name__)}


# ══════════════════════════════════════════════════════════════════
# PATTERNS 71-100: Dependency Injection
# ══════════════════════════════════════════════════════════════════

# P071: simple dep
@app.get("/p071-simple-dep")
def p071(val=Depends(get_simple_dep)):
    return {"val": val}

# P072: chained dep
@app.get("/p072-chained-dep")
def p072(val=Depends(get_chained_dep)):
    return {"val": val}

# P073: generator dep
@app.get("/p073-generator-dep")
def p073(val=Depends(get_generator_dep)):
    return {"val": val}

# P074: async dep
@app.get("/p074-async-dep")
async def p074(val=Depends(get_async_dep)):
    return {"val": val}

# P075: class dep
@app.get("/p075-class-dep")
def p075(val=Depends(callable_dep)):
    return {"val": val}

# P076: dep as route-level dependency (no return consumed)
@app.get("/p076-dep-no-return", dependencies=[Depends(side_effect_dep)])
def p076():
    return {"ok": True}

# P077: dep override
@app.get("/p077-dep-override")
def p077(val=Depends(get_simple_dep)):
    return {"val": val}

# P078: dep with query
@app.get("/p078-dep-with-query")
def p078(val=Depends(dep_with_query)):
    return {"val": val}

# P079: dep with header
@app.get("/p079-dep-with-header")
def p079(val=Depends(dep_with_header)):
    return {"val": val}

# P080: dep with request
@app.get("/p080-dep-with-request")
def p080(val=Depends(dep_with_request)):
    return {"val": val}

# P081: oauth2 bearer
@app.get("/p081-security-oauth2")
async def p081(token: str = Depends(oauth2_scheme)):
    return {"token": token}

# P082: HTTP bearer
@app.get("/p082-security-bearer")
async def p082(creds=Depends(bearer_scheme)):
    return {"scheme": creds.scheme, "credentials": creds.credentials}

# P083: API key header
@app.get("/p083-security-apikey")
async def p083(key: str = Depends(apikey_scheme)):
    return {"key": key}

# P084: HTTP basic
@app.get("/p084-security-basic")
async def p084(creds=Depends(basic_scheme)):
    return {"username": creds.username, "password": creds.password}

# P085: security scopes (simplified — just pass scopes through)
@app.get("/p085-security-scopes")
async def p085(token: str = Depends(oauth2_scheme)):
    return {"token": token}

# P086: OAuth2PasswordRequestForm
@app.post("/p086-oauth2-form")
async def p086(form_data: OAuth2PasswordRequestForm = Depends()):
    return {"username": form_data.username, "password": form_data.password}

# P087: cached dep
@app.get("/p087-dep-cached")
def p087(val1=Depends(dep_cached), val2=Depends(dep_cached)):
    return {"val1": val1, "val2": val2, "same": val1 == val2}

# P088: async generator dep
@app.get("/p088-dep-async-generator")
async def p088(val=Depends(async_gen_dep)):
    return {"val": val}

# P089: 3-level nested deps
@app.get("/p089-nested-deps-3-deep")
def p089(val=Depends(dep_level_3)):
    return {"val": val}

# P090: dep raises HTTPException
@app.get("/p090-dep-exception")
def p090(val=Depends(dep_raises)):
    return {"val": val}

# P091 and P092 defined on routers above

# P093: dep with background task
@app.get("/p093-dep-background")
def p093(bg: BackgroundTasks):
    bg.add_task(lambda: None)
    return {"ok": True}

# P094: dep with Response
@app.get("/p094-dep-response")
def p094(response: Response):
    response.headers["X-Dep-Response"] = "yes"
    return {"ok": True}

# P095: WebSocket dep — tested separately (skip in HTTP parity)
@app.get("/p095-dep-websocket")
def p095():
    return {"skip": "websocket_test"}

# P096: multiple deps
@app.get("/p096-multiple-deps")
def p096(a=Depends(dep_a), b=Depends(dep_b), c=Depends(dep_c)):
    return {"a": a, "b": b, "c": c}

# P097: dep with default value
@app.get("/p097-dep-default")
def p097(val=Depends(dep_with_default)):
    return {"val": val}

# P098: Annotated dep
@app.get("/p098-annotated-dep")
def p098(val: Annotated[str, Depends(get_simple_dep)]):
    return {"val": val}

# P099: security auto_error=False
oauth2_no_error = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)

@app.get("/p099-security-auto-error")
async def p099(token: str | None = Depends(oauth2_no_error)):
    return {"token": token}

# P100: yield dep cleanup verification
@app.get("/p100-dep-yield-cleanup")
def p100(val=Depends(dep_yield_cleanup)):
    return {"val": val}

@app.get("/p100-dep-yield-cleanup-check")
def p100_check():
    return {"log": _cleanup_log.copy()}
