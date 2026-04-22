"""Deep schema parity mega-app: extensive permutations for OpenAPI structural comparison.

Uses ONLY stock FastAPI imports. The compat shim maps these to fastapi-rs.
All endpoints here are intended to produce structurally identical OpenAPI
schemas under FastAPI (uvicorn) and fastapi-rs.
"""
import enum
from typing import Annotated, Dict, List, Literal, Optional, Union
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBearer,
    HTTPDigest,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
)
from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────

class Color(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"


class Priority(int, enum.Enum):
    low = 1
    med = 2
    high = 3


class Status(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    pending = "pending"


# ── Basic models ─────────────────────────────────────────────────────

class Item(BaseModel):
    name: str
    price: float
    description: Optional[str] = None


class ItemOut(BaseModel):
    name: str
    price: float


class User(BaseModel):
    id: int
    username: str
    email: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str


class Address(BaseModel):
    street: str
    city: str
    zip: str


class Profile(BaseModel):
    user: User
    address: Address
    bio: Optional[str] = None


# ── Constrained fields ───────────────────────────────────────────────

class ConstrainedItem(BaseModel):
    name: str = Field(min_length=3, max_length=50)
    price: float = Field(gt=0, lt=10000)
    quantity: int = Field(ge=0, le=999)
    code: str = Field(pattern=r"^[A-Z]{2,4}$")
    description: str = Field(default="", description="An item description")


class WithDefaults(BaseModel):
    name: str = "default_name"
    value: int = 42
    active: bool = True
    tags: List[str] = []


class WithAliases(BaseModel):
    item_name: str = Field(alias="itemName")
    item_id: int = Field(alias="itemId")


class WithExamples(BaseModel):
    count: int = Field(examples=[1, 2, 3])
    label: str = Field(examples=["alpha"])


# ── Nested / composition ─────────────────────────────────────────────

class Tag(BaseModel):
    name: str
    color: Color


class Article(BaseModel):
    title: str
    author: User
    tags: List[Tag] = []
    meta: Dict[str, str] = {}


class ListContainer(BaseModel):
    items: List[Item]


class DictContainer(BaseModel):
    items: Dict[str, Item]


class NestedOptional(BaseModel):
    profile: Optional[Profile] = None


# ── Discriminated unions ─────────────────────────────────────────────

class Cat(BaseModel):
    pet_type: Literal["cat"]
    meows: int


class Dog(BaseModel):
    pet_type: Literal["dog"]
    barks: float


class Lizard(BaseModel):
    pet_type: Literal["lizard"]
    scales: bool


Pet = Annotated[Union[Cat, Dog, Lizard], Field(discriminator="pet_type")]


class PetOwner(BaseModel):
    name: str
    pet: Pet


# ── Inheritance ──────────────────────────────────────────────────────

class BaseEntity(BaseModel):
    id: int
    created_at: Optional[str] = None


class Product(BaseEntity):
    name: str
    price: float


class DigitalProduct(Product):
    download_url: str


# ── Optional / union-of-none ─────────────────────────────────────────

class OptionalFields(BaseModel):
    required: str
    optional_int: Optional[int] = None
    optional_str: Optional[str] = None
    union_field: Union[str, int, None] = None


# ── Generic-ish simple wrappers ──────────────────────────────────────

class Page(BaseModel):
    total: int
    items: List[Item]


class ErrorResponse(BaseModel):
    code: int
    message: str
    details: Optional[Dict[str, str]] = None


# ── Security schemes ─────────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token", scheme_name="oauth2pwd")
bearer_scheme = HTTPBearer(scheme_name="bearerAuth")
basic_scheme = HTTPBasic(scheme_name="basicAuth")
digest_scheme = HTTPDigest(scheme_name="digestAuth")
apikey_header = APIKeyHeader(name="X-API-Key", scheme_name="apiKeyHeader")
apikey_query = APIKeyQuery(name="api_key", scheme_name="apiKeyQuery")
apikey_cookie = APIKeyCookie(name="api_key_cookie", scheme_name="apiKeyCookie")


# ── Dependencies ─────────────────────────────────────────────────────

def common_params(skip: int = 0, limit: int = 10):
    return {"skip": skip, "limit": limit}


def get_current_user(token: str = Depends(oauth2_scheme)):
    return {"token": token}


def side_dep():
    return True


# ── App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="deep-schema-parity",
    version="1.0.0",
    description="Deep OpenAPI structural parity test surface",
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════
# S001-S020: basic CRUD with response models, status codes, tags
# ══════════════════════════════════════════════════════════════════════

@app.get("/s001-list-items", response_model=List[Item], tags=["items"], summary="List items",
         description="Returns a list of items", operation_id="s001_list_items")
def s001():
    return []


@app.get("/s002-get-item/{item_id}", response_model=Item, tags=["items"],
         summary="Get item", operation_id="s002_get_item")
def s002(item_id: int):
    return Item(name="x", price=1.0)


@app.post("/s003-create-item", response_model=Item, status_code=201, tags=["items"],
          summary="Create item", operation_id="s003_create_item")
def s003(item: Item):
    return item


@app.put("/s004-update-item/{item_id}", response_model=Item, tags=["items"],
         operation_id="s004_update_item")
def s004(item_id: int, item: Item):
    return item


@app.patch("/s005-patch-item/{item_id}", response_model=Item, tags=["items"],
           operation_id="s005_patch_item")
def s005(item_id: int, item: Item):
    return item


@app.delete("/s006-delete-item/{item_id}", status_code=204, tags=["items"],
            operation_id="s006_delete_item")
def s006(item_id: int):
    return None


@app.get("/s007-list-users", response_model=List[UserOut], tags=["users"],
        operation_id="s007_list_users")
def s007():
    return []


@app.get("/s008-get-user/{user_id}", response_model=UserOut, tags=["users"],
        operation_id="s008_get_user")
def s008(user_id: int):
    return UserOut(id=user_id, username="x")


@app.post("/s009-create-user", response_model=UserOut, status_code=201, tags=["users"],
         operation_id="s009_create_user")
def s009(user: User):
    return user


@app.get("/s010-get-profile/{user_id}", response_model=Profile, tags=["users"],
        operation_id="s010_get_profile")
def s010(user_id: int):
    return None  # pragma: no cover


@app.post("/s011-create-profile", response_model=Profile, tags=["users"],
         operation_id="s011_create_profile")
def s011(p: Profile):
    return p


@app.get("/s012-article-list", response_model=List[Article], tags=["articles"],
        operation_id="s012_articles")
def s012():
    return []


@app.post("/s013-article", response_model=Article, tags=["articles"],
         operation_id="s013_create_article")
def s013(a: Article):
    return a


@app.get("/s014-page", response_model=Page, operation_id="s014_page")
def s014():
    return Page(total=0, items=[])


@app.post("/s015-list-container", response_model=ListContainer,
         operation_id="s015_list_container")
def s015(body: ListContainer):
    return body


@app.post("/s016-dict-container", response_model=DictContainer,
         operation_id="s016_dict_container")
def s016(body: DictContainer):
    return body


@app.post("/s017-nested-optional", response_model=NestedOptional,
         operation_id="s017_nested_optional")
def s017(body: NestedOptional):
    return body


@app.post("/s018-constrained", response_model=ConstrainedItem,
         operation_id="s018_constrained")
def s018(body: ConstrainedItem):
    return body


@app.post("/s019-defaults", response_model=WithDefaults,
         operation_id="s019_defaults")
def s019(body: WithDefaults):
    return body


@app.post("/s020-aliases", response_model=WithAliases, operation_id="s020_aliases")
def s020(body: WithAliases):
    return body


# ══════════════════════════════════════════════════════════════════════
# S021-S040: query constraints and param styles
# ══════════════════════════════════════════════════════════════════════

@app.get("/s021-q-ge")
def s021(n: int = Query(ge=0)):
    return {"n": n}


@app.get("/s022-q-le")
def s022(n: int = Query(le=100)):
    return {"n": n}


@app.get("/s023-q-gt")
def s023(n: int = Query(gt=0)):
    return {"n": n}


@app.get("/s024-q-lt")
def s024(n: int = Query(lt=100)):
    return {"n": n}


@app.get("/s025-q-ge-le")
def s025(n: int = Query(ge=0, le=100)):
    return {"n": n}


@app.get("/s026-q-gt-lt")
def s026(n: int = Query(gt=0, lt=100)):
    return {"n": n}


@app.get("/s027-q-min-length")
def s027(s: str = Query(min_length=3)):
    return {"s": s}


@app.get("/s028-q-max-length")
def s028(s: str = Query(max_length=10)):
    return {"s": s}


@app.get("/s029-q-min-max-length")
def s029(s: str = Query(min_length=3, max_length=10)):
    return {"s": s}


@app.get("/s030-q-pattern")
def s030(code: str = Query(pattern=r"^[A-Z]+$")):
    return {"code": code}


@app.get("/s031-q-default-int")
def s031(n: int = Query(default=10)):
    return {"n": n}


@app.get("/s032-q-default-str")
def s032(s: str = Query(default="hello")):
    return {"s": s}


@app.get("/s033-q-default-bool")
def s033(b: bool = Query(default=False)):
    return {"b": b}


@app.get("/s034-q-optional")
def s034(q: Optional[str] = Query(default=None)):
    return {"q": q}


@app.get("/s035-q-required")
def s035(q: str = Query()):
    return {"q": q}


@app.get("/s036-q-list")
def s036(items: List[str] = Query()):
    return {"items": items}


@app.get("/s037-q-list-default")
def s037(items: List[str] = Query(default=[])):
    return {"items": items}


@app.get("/s038-q-int-list")
def s038(nums: List[int] = Query()):
    return {"nums": nums}


@app.get("/s039-q-alias")
def s039(q: str = Query(alias="user-query")):
    return {"q": q}


@app.get("/s040-q-description")
def s040(q: str = Query(description="Search query string")):
    return {"q": q}


# ══════════════════════════════════════════════════════════════════════
# S041-S060: path params, typed variations, header/cookie
# ══════════════════════════════════════════════════════════════════════

@app.get("/s041-p-int/{item_id}")
def s041(item_id: int):
    return {"id": item_id}


@app.get("/s042-p-str/{name}")
def s042(name: str):
    return {"name": name}


@app.get("/s043-p-float/{price}")
def s043(price: float):
    return {"price": price}


@app.get("/s044-p-bool/{flag}")
def s044(flag: bool):
    return {"flag": flag}


@app.get("/s045-p-ge/{n}")
def s045(n: int = Path(ge=0)):
    return {"n": n}


@app.get("/s046-p-le/{n}")
def s046(n: int = Path(le=1000)):
    return {"n": n}


@app.get("/s047-p-range/{n}")
def s047(n: int = Path(ge=1, le=100)):
    return {"n": n}


@app.get("/s048-p-description/{n}")
def s048(n: int = Path(description="The ID of the thing")):
    return {"n": n}


@app.get("/s049-p-enum/{color}")
def s049(color: Color):
    return {"color": color}


@app.get("/s050-p-literal/{val}")
def s050(val: Literal["a", "b", "c"]):
    return {"val": val}


@app.get("/s051-header-basic")
def s051(x_token: str = Header()):
    return {"x_token": x_token}


@app.get("/s052-header-default")
def s052(x_custom: str = Header(default="def")):
    return {"x_custom": x_custom}


@app.get("/s053-header-optional")
def s053(x_opt: Optional[str] = Header(default=None)):
    return {"x_opt": x_opt}


@app.get("/s054-header-alias")
def s054(token: str = Header(alias="X-Auth-Token")):
    return {"token": token}


@app.get("/s055-header-description")
def s055(x_token: str = Header(description="The auth token")):
    return {"x_token": x_token}


@app.get("/s056-cookie-basic")
def s056(session: str = Cookie()):
    return {"session": session}


@app.get("/s057-cookie-default")
def s057(session: str = Cookie(default="anon")):
    return {"session": session}


@app.get("/s058-cookie-optional")
def s058(session: Optional[str] = Cookie(default=None)):
    return {"session": session}


@app.get("/s059-cookie-description")
def s059(session: str = Cookie(description="Session cookie")):
    return {"session": session}


@app.get("/s060-mixed-params/{item_id}")
def s060(
    item_id: int,
    q: str = Query(default="hi"),
    x_token: str = Header(default="nothing"),
    session: str = Cookie(default="anon"),
):
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# S061-S080: body / request body schema variations
# ══════════════════════════════════════════════════════════════════════

@app.post("/s061-body-model")
def s061(item: Item):
    return item


@app.post("/s062-body-embed")
def s062(item: Item = Body(embed=True)):
    return item


@app.post("/s063-body-multi")
def s063(item: Item = Body(), extra: str = Body()):
    return {"item": item, "extra": extra}


@app.post("/s064-body-optional")
def s064(item: Optional[Item] = Body(default=None)):
    return item


@app.post("/s065-body-list")
def s065(items: List[Item]):
    return items


@app.post("/s066-body-dict")
def s066(mapping: Dict[str, Item]):
    return mapping


@app.post("/s067-body-nested")
def s067(profile: Profile):
    return profile


@app.post("/s068-body-discriminated")
def s068(owner: PetOwner):
    return owner


@app.post("/s069-body-inherited")
def s069(d: DigitalProduct):
    return d


@app.post("/s070-body-optional-fields")
def s070(body: OptionalFields):
    return body


@app.post("/s071-body-constrained")
def s071(body: ConstrainedItem):
    return body


@app.post("/s072-body-with-defaults")
def s072(body: WithDefaults):
    return body


@app.post("/s073-body-aliased")
def s073(body: WithAliases):
    return body


@app.post("/s074-body-examples")
def s074(body: WithExamples):
    return body


@app.post("/s075-body-tag-list")
def s075(tags: List[Tag]):
    return tags


@app.post("/s076-form-basic")
def s076(username: str = Form(), password: str = Form()):
    return {"ok": True}


@app.post("/s077-file-basic")
def s077(file: bytes = File()):
    return {"size": len(file)}


@app.post("/s078-uploadfile")
def s078(file: UploadFile):
    return {"filename": file.filename}


@app.post("/s079-form-file")
def s079(name: str = Form(), file: UploadFile = File()):
    return {"name": name}


@app.post("/s080-oauth2-form")
def s080(form: OAuth2PasswordRequestForm = Depends()):
    return {"u": form.username}


# ══════════════════════════════════════════════════════════════════════
# S081-S100: responses, status codes, media types, deprecated
# ══════════════════════════════════════════════════════════════════════

@app.get("/s081-deprecated", deprecated=True)
def s081():
    return {"deprecated": True}


@app.get("/s082-non-deprecated", deprecated=False)
def s082():
    return {"ok": True}


@app.get("/s083-tags-multi", tags=["alpha", "beta"])
def s083():
    return {"ok": True}


@app.get("/s084-summary", summary="Does a thing")
def s084():
    return {"ok": True}


@app.get("/s085-description", description="A longer description of the endpoint")
def s085():
    return {"ok": True}


@app.get("/s086-operation-id", operation_id="custom_op_id_s086")
def s086():
    return {"ok": True}


@app.get("/s087-status-201", status_code=201)
def s087():
    return {"ok": True}


@app.get("/s088-status-202", status_code=202)
def s088():
    return {"ok": True}


@app.get("/s089-status-204", status_code=204)
def s089():
    return None


@app.get("/s090-status-418", status_code=418)
def s090():
    return {"teapot": True}


@app.get("/s091-multiple-responses", responses={
    404: {"model": ErrorResponse, "description": "Not found"},
    500: {"model": ErrorResponse, "description": "Server error"},
})
def s091():
    return {"ok": True}


@app.get("/s092-response-class", response_class=HTMLResponse)
def s092():
    return "<h1>hi</h1>"


@app.get("/s093-response-class-plain", response_class=PlainTextResponse)
def s093():
    return "plain"


@app.get("/s094-response-model-exclude", response_model=Item, response_model_exclude_unset=True)
def s094():
    return Item(name="x", price=1.0)


@app.get("/s095-response-model-exclude-none", response_model=Item,
         response_model_exclude_none=True)
def s095():
    return Item(name="x", price=1.0)


@app.get("/s096-response-model-include", response_model=Item,
         response_model_include={"name"})
def s096():
    return Item(name="x", price=1.0)


@app.get("/s097-response-model-exclude-fields", response_model=Item,
         response_model_exclude={"description"})
def s097():
    return Item(name="x", price=1.0)


@app.get("/s098-combined", response_model=Item, status_code=201, tags=["alpha"],
         summary="Combined", description="Combined endpoint", deprecated=True,
         operation_id="s098_combined")
def s098():
    return Item(name="x", price=1.0)


@app.get("/s099-no-response-model")
def s099():
    return {"ok": True}


@app.get("/s100-enum-query-param")
def s100(c: Color = Query()):
    return {"c": c}


# ══════════════════════════════════════════════════════════════════════
# S101-S120: security + dependencies + router inclusion
# ══════════════════════════════════════════════════════════════════════

@app.get("/s101-oauth2", tags=["auth"])
def s101(token: str = Depends(oauth2_scheme)):
    return {"token": token}


@app.get("/s102-bearer", tags=["auth"])
def s102(creds=Depends(bearer_scheme)):
    return {"ok": True}


@app.get("/s103-basic", tags=["auth"])
def s103(creds=Depends(basic_scheme)):
    return {"ok": True}


@app.get("/s104-digest", tags=["auth"])
def s104(creds=Depends(digest_scheme)):
    return {"ok": True}


@app.get("/s105-apikey-header", tags=["auth"])
def s105(k: str = Depends(apikey_header)):
    return {"ok": True}


@app.get("/s106-apikey-query", tags=["auth"])
def s106(k: str = Depends(apikey_query)):
    return {"ok": True}


@app.get("/s107-apikey-cookie", tags=["auth"])
def s107(k: str = Depends(apikey_cookie)):
    return {"ok": True}


@app.get("/s108-multi-security")
def s108(tk: str = Depends(oauth2_scheme), k: str = Depends(apikey_header)):
    return {"ok": True}


@app.get("/s109-common-deps")
def s109(commons: dict = Depends(common_params)):
    return commons


@app.get("/s110-user-dep")
def s110(user: dict = Depends(get_current_user)):
    return user


@app.get("/s111-route-level-dep", dependencies=[Depends(side_dep)])
def s111():
    return {"ok": True}


@app.get("/s112-multi-route-deps",
         dependencies=[Depends(side_dep), Depends(common_params)])
def s112():
    return {"ok": True}


# Sub-router
sub = APIRouter(prefix="/sub", tags=["sub"])


@sub.get("/s113-sub-get")
def s113():
    return {"ok": True}


@sub.post("/s114-sub-post", response_model=Item)
def s114(item: Item):
    return item


@sub.get("/s115-sub-path/{x}")
def s115(x: int):
    return {"x": x}


app.include_router(sub)


# Router with dependencies + tags propagation
auth_router = APIRouter(prefix="/auth-area", tags=["secured"],
                        dependencies=[Depends(side_dep)])


@auth_router.get("/s116")
def s116():
    return {"ok": True}


@auth_router.post("/s117", response_model=ItemOut)
def s117(item: Item):
    return item


app.include_router(auth_router)


# ══════════════════════════════════════════════════════════════════════
# S118-S150: more permutations for coverage breadth
# ══════════════════════════════════════════════════════════════════════

@app.get("/s118-literal-query")
def s118(mode: Literal["fast", "slow"] = Query(default="fast")):
    return {"mode": mode}


@app.get("/s119-literal-int-query")
def s119(code: Literal[1, 2, 3] = Query(default=1)):
    return {"code": code}


@app.get("/s120-priority-enum")
def s120(p: Priority = Query()):
    return {"p": p}


@app.get("/s121-status-enum")
def s121(s: Status = Query()):
    return {"s": s}


@app.post("/s122-uuid-body")
def s122(uid: UUID = Body()):
    return {"uid": str(uid)}


@app.get("/s123-uuid-path/{uid}")
def s123(uid: UUID):
    return {"uid": str(uid)}


@app.get("/s124-str-query-default-empty")
def s124(b: str = Query(default="")):
    return {"ok": True}


@app.post("/s125-body-any")
def s125(payload: dict = Body()):
    return payload


@app.post("/s126-body-any-list")
def s126(payload: list = Body()):
    return payload


@app.post("/s127-body-annotated")
def s127(item: Annotated[Item, Body()]):
    return item


@app.post("/s128-body-annotated-embed")
def s128(item: Annotated[Item, Body(embed=True)]):
    return item


@app.get("/s129-annotated-query")
def s129(q: Annotated[str, Query(min_length=1, max_length=20)]):
    return {"q": q}


@app.get("/s130-annotated-header")
def s130(tok: Annotated[str, Header()]):
    return {"tok": tok}


@app.get("/s131-annotated-path/{x}")
def s131(x: Annotated[int, Path(ge=1)]):
    return {"x": x}


@app.get("/s132-annotated-cookie")
def s132(c: Annotated[str, Cookie()]):
    return {"c": c}


@app.post("/s133-response-wildcard",
          responses={"4XX": {"model": ErrorResponse}})
def s133():
    return {"ok": True}


@app.get("/s134-response-default",
         responses={"default": {"model": ErrorResponse}})
def s134():
    return {"ok": True}


@app.get("/s135-nested-dict-return", response_model=Dict[str, List[int]])
def s135():
    return {}


@app.get("/s136-list-of-lists", response_model=List[List[int]])
def s136():
    return []


@app.get("/s137-dict-of-dict", response_model=Dict[str, Dict[str, int]])
def s137():
    return {}


@app.get("/s138-union-response", response_model=Union[Item, ItemOut])
def s138():
    return ItemOut(name="x", price=1.0)


@app.get("/s139-optional-response", response_model=Optional[Item])
def s139():
    return None


@app.post("/s140-pet-owner", response_model=PetOwner)
def s140(owner: PetOwner):
    return owner


@app.get("/s141-literal-response",
         response_model=Literal["ok", "err"])
def s141():
    return "ok"


@app.get("/s142-inherited-return", response_model=DigitalProduct)
def s142():
    return None


@app.post("/s143-multi-complex",
          response_model=Page, status_code=201, tags=["search"],
          operation_id="s143_multi_complex",
          responses={404: {"model": ErrorResponse, "description": "Not found"}})
def s143(item: Item, q: str = Query(min_length=1)):
    return Page(total=0, items=[])


@app.post("/s144-form-multi")
def s144(a: str = Form(), b: int = Form(), c: bool = Form(default=False)):
    return {"a": a, "b": b, "c": c}


@app.post("/s145-files-list")
def s145(files: List[UploadFile] = File()):
    return {"count": len(files)}


@app.post("/s146-bytes-list")
def s146(files: List[bytes] = File()):
    return {"count": len(files)}


@app.get("/s147-header-list")
def s147(x_tokens: List[str] = Header()):
    return {"tokens": x_tokens}


@app.get("/s148-query-int-default-0")
def s148(n: int = Query(default=0)):
    return {"n": n}


@app.get("/s149-query-float")
def s149(x: float = Query()):
    return {"x": x}


@app.get("/s150-query-float-constrained")
def s150(x: float = Query(gt=0.0, lt=1.0)):
    return {"x": x}
