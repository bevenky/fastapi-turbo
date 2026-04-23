"""Round 2 Deep schema parity mega-app.

This app is designed to exercise EVERY corner of OpenAPI schema generation.
The Round 2 runner performs full deep-subtree equality on every node, so the
richer we make the schema, the more gaps it surfaces.

Only stock FastAPI + Pydantic imports. The compat shim redirects imports to
fastapi-turbo when executed under jamun.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import (
    Annotated,
    Any,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)
from uuid import UUID, uuid4

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
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBearer,
    HTTPDigest,
    OAuth2,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OpenIdConnect,
    SecurityScopes,
)
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
)


# ══════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════

class Color(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"


class Priority(int, enum.Enum):
    low = 1
    medium = 2
    high = 3


class Severity(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


# ══════════════════════════════════════════════════════════════════════
# Models with deep schema features
# ══════════════════════════════════════════════════════════════════════

class Address(BaseModel):
    street: str = Field(..., description="Street line", examples=["123 Main St"])
    city: str = Field(..., min_length=1, max_length=100)
    zipcode: str = Field(..., pattern=r"^\d{5}$", examples=["94105"])
    country: str = Field(default="US", min_length=2, max_length=2)


class Tag(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"name": "urgent", "color": "red"},
                {"name": "later", "color": "blue"},
            ]
        }
    )
    name: str = Field(..., min_length=1)
    color: Color = Color.red


class Item(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, examples=["Widget"])
    price: float = Field(..., gt=0, lt=1_000_000, description="USD price")
    description: Optional[str] = Field(
        default=None, max_length=1000, examples=["A fine widget"]
    )
    tags: List[Tag] = Field(default_factory=list)
    address: Optional[Address] = None
    stock: int = Field(default=0, ge=0, le=100_000)
    ratio: float = Field(default=0.5, ge=0.0, le=1.0, examples=[0.3, 0.7])


class ItemOut(BaseModel):
    id: UUID
    name: str
    price: float
    tags: List[Tag] = Field(default_factory=list)


class User(BaseModel):
    id: int = Field(..., ge=1)
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^\w+$")
    email: Optional[str] = Field(default=None, examples=["a@b.com"])
    age: Optional[int] = Field(default=None, ge=0, le=150)
    is_active: bool = True
    created_at: Optional[datetime] = None


class UserCreate(User):
    password: str = Field(..., min_length=8, max_length=128)


class UserPublic(BaseModel):
    id: int
    username: str
    is_active: bool


# Alias/by_alias model
class AliasModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_name: str = Field(..., alias="userName", examples=["bob"])
    full_email: str = Field(..., alias="fullEmail")


# Recursive model
class Node(BaseModel):
    name: str
    value: Optional[int] = None
    children: List["Node"] = Field(default_factory=list)


Node.model_rebuild()


# Tree container (nested recursive refs)
class Forest(BaseModel):
    roots: List[Node]
    total: int = Field(default=0, ge=0)


# Models with computed fields
class Circle(BaseModel):
    radius: float = Field(..., gt=0)

    @computed_field
    @property
    def area(self) -> float:
        return 3.14159 * self.radius * self.radius

    @computed_field
    @property
    def circumference(self) -> float:
        return 2 * 3.14159 * self.radius


# Generic model
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int = Field(..., ge=0)
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=500)


# Discriminated union
class Cat(BaseModel):
    pet_type: Literal["cat"] = "cat"
    meow_volume: int = Field(default=5, ge=0, le=10)
    indoor: bool = True


class Dog(BaseModel):
    pet_type: Literal["dog"] = "dog"
    bark_volume: int = Field(default=5, ge=0, le=10)
    breed: Optional[str] = None


class Bird(BaseModel):
    pet_type: Literal["bird"] = "bird"
    can_fly: bool = True
    species: str = "sparrow"


PetUnion = Annotated[Union[Cat, Dog, Bird], Field(discriminator="pet_type")]


class PetOwner(BaseModel):
    name: str
    pet: PetUnion


# Deep nested
class Company(BaseModel):
    name: str
    founded: int = Field(..., ge=1800, le=2100)
    ceo: User
    employees: List[User] = Field(default_factory=list)
    headquarters: Address
    tags: Dict[str, Tag] = Field(default_factory=dict)


# Error models
class ErrorDetail(BaseModel):
    code: str = Field(..., examples=["NOT_FOUND"])
    message: str
    field: Optional[str] = None


class APIError(BaseModel):
    error: ErrorDetail
    request_id: str = Field(..., examples=["req-123"])


# Model with json_schema_extra at field level
class Slider(BaseModel):
    value: Annotated[
        int,
        Field(
            ge=0,
            le=100,
            examples=[25, 50, 75],
            json_schema_extra={"x-ui-widget": "slider", "x-ui-step": 5},
        ),
    ]
    label: Annotated[
        str,
        Field(
            default="",
            json_schema_extra={"x-ui-widget": "text", "x-ui-placeholder": "Label..."},
        ),
    ]


# Model with multiple formats
class Artifact(BaseModel):
    id: UUID
    created: datetime
    updated: Optional[datetime] = None
    expires_at: Optional[date] = None
    ttl: Optional[timedelta] = None
    url: Optional[HttpUrl] = None
    email: Optional[str] = None
    amount: Decimal = Field(default=Decimal("0"))


# Model with deprecated field
class LegacyModel(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"x-deprecated-reason": "use NewModel"}
    )
    old_id: int
    new_id: Optional[int] = Field(default=None, description="Replacement id")


# Literal enums
class Status(BaseModel):
    state: Literal["queued", "running", "done", "failed"] = "queued"
    attempt: int = Field(default=1, ge=1, le=10)


# ══════════════════════════════════════════════════════════════════════
# Security schemes
# ══════════════════════════════════════════════════════════════════════

http_basic = HTTPBasic(description="Basic auth", scheme_name="HTTPBasicAuth")
http_bearer = HTTPBearer(bearerFormat="JWT", description="Bearer token")
http_digest = HTTPDigest(description="Digest auth")
api_key_header = APIKeyHeader(name="X-API-Key", description="API key in header")
api_key_query = APIKeyQuery(name="api_key", description="API key in query")
api_key_cookie = APIKeyCookie(name="session", description="Session cookie")
oauth2_password = OAuth2PasswordBearer(
    tokenUrl="token",
    scopes={"read": "Read items", "write": "Write items", "admin": "Admin access"},
    description="OAuth2 password flow",
    scheme_name="OAuth2Password",
)
oauth2_authcode = OAuth2AuthorizationCodeBearer(
    authorizationUrl="https://example.com/oauth/authorize",
    tokenUrl="https://example.com/oauth/token",
    refreshUrl="https://example.com/oauth/refresh",
    scopes={"profile": "Profile", "email": "Email"},
    description="OAuth2 authorization code flow",
    scheme_name="OAuth2AuthCode",
)
oauth2_multi = OAuth2(
    flows={
        "password": {
            "tokenUrl": "/token-pw",
            "scopes": {"r": "read", "w": "write"},
        },
        "clientCredentials": {
            "tokenUrl": "/token-cc",
            "scopes": {"svc": "service calls"},
        },
        "authorizationCode": {
            "authorizationUrl": "https://ex.com/authz",
            "tokenUrl": "https://ex.com/token",
            "scopes": {"p": "profile", "e": "email", "x": "extra"},
        },
    },
    scheme_name="OAuth2Multi",
    description="OAuth2 with three flows",
)
openid = OpenIdConnect(
    openIdConnectUrl="https://example.com/.well-known/openid-configuration",
    description="OpenID Connect",
    scheme_name="OpenIDC",
)


# ══════════════════════════════════════════════════════════════════════
# Dependencies
# ══════════════════════════════════════════════════════════════════════

def common_pagination(
    offset: int = Query(0, ge=0, description="Offset"),
    limit: int = Query(20, ge=1, le=500, description="Limit"),
) -> Dict[str, int]:
    return {"offset": offset, "limit": limit}


def require_api_key(key: str = Depends(api_key_header)) -> str:
    return key


# ══════════════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Deep Schema R2",
    version="2.0.0",
    description="Round 2 deep schema parity app",
    summary="R2 app summary",
    terms_of_service="https://example.com/tos",
    contact={
        "name": "Support",
        "url": "https://example.com/support",
        "email": "support@example.com",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
        "identifier": "MIT",
    },
    servers=[
        {"url": "https://api.example.com", "description": "Production"},
        {"url": "https://staging.example.com", "description": "Staging"},
        {"url": "/", "description": "Local"},
    ],
    openapi_tags=[
        {
            "name": "items",
            "description": "Item operations",
            "externalDocs": {
                "url": "https://example.com/docs/items",
                "description": "Item docs",
            },
        },
        {
            "name": "users",
            "description": "User operations",
        },
        {
            "name": "auth",
            "description": "Authentication endpoints",
            "externalDocs": {"url": "https://example.com/docs/auth"},
        },
        {"name": "search", "description": "Search"},
        {"name": "admin", "description": "Admin ops"},
        {"name": "forms", "description": "Form / multipart"},
        {"name": "files", "description": "File uploads"},
        {"name": "generic", "description": "Generic & recursive"},
        {"name": "special", "description": "Special response shapes"},
    ],
)


# ══════════════════════════════════════════════════════════════════════
# Item endpoints — many response variations
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/items",
    response_model=List[Item],
    tags=["items"],
    summary="List items",
    description="Return a list of all items",
    operation_id="list_items",
    responses={
        200: {"description": "OK"},
        401: {"description": "Unauthorized", "model": APIError},
        403: {"description": "Forbidden", "model": APIError},
    },
)
def list_items(pagination: Dict[str, int] = Depends(common_pagination)) -> List[Item]:
    return []


@app.get(
    "/items/{item_id}",
    response_model=Item,
    tags=["items"],
    operation_id="get_item",
    responses={
        200: {"description": "OK"},
        404: {"description": "Not found", "model": APIError},
        410: {"description": "Gone", "model": APIError},
    },
)
def get_item(
    item_id: UUID = Path(..., description="The item id", examples=["6f7b..."]),
) -> Item:
    return Item(name="x", price=1)


@app.post(
    "/items",
    response_model=ItemOut,
    status_code=201,
    tags=["items"],
    operation_id="create_item",
    responses={
        201: {"description": "Created"},
        400: {"description": "Bad request", "model": APIError},
        422: {"description": "Validation error"},
    },
)
def create_item(item: Item) -> ItemOut:
    return ItemOut(id=uuid4(), name=item.name, price=item.price)


@app.put(
    "/items/{item_id}",
    response_model=ItemOut,
    tags=["items"],
    operation_id="replace_item",
)
def replace_item(item_id: UUID, item: Item) -> ItemOut:
    return ItemOut(id=item_id, name=item.name, price=item.price)


@app.patch(
    "/items/{item_id}",
    response_model=ItemOut,
    tags=["items"],
    operation_id="patch_item",
)
def patch_item(item_id: UUID, item: Item) -> ItemOut:
    return ItemOut(id=item_id, name=item.name, price=item.price)


@app.delete(
    "/items/{item_id}",
    status_code=204,
    tags=["items"],
    operation_id="delete_item",
    responses={
        204: {"description": "Deleted"},
        404: {"description": "Not found", "model": APIError},
    },
)
def delete_item(item_id: UUID) -> None:
    return None


@app.get(
    "/items/{item_id}/deprecated",
    response_model=Item,
    tags=["items"],
    deprecated=True,
    operation_id="get_item_deprecated",
    responses={
        200: {"description": "OK"},
        410: {"description": "Gone"},
    },
)
def get_item_deprecated(item_id: UUID) -> Item:
    return Item(name="x", price=1)


# ══════════════════════════════════════════════════════════════════════
# User endpoints — security variations
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/users",
    response_model=List[UserPublic],
    tags=["users"],
    operation_id="list_users",
)
def list_users(
    pagination: Dict[str, int] = Depends(common_pagination),
    _key: str = Depends(require_api_key),
) -> List[UserPublic]:
    return []


@app.get(
    "/users/{user_id}",
    response_model=UserPublic,
    tags=["users"],
    operation_id="get_user",
    responses={
        200: {"description": "OK"},
        404: {"description": "Not found", "model": APIError},
    },
)
def get_user(user_id: int = Path(..., ge=1)) -> UserPublic:
    return UserPublic(id=user_id, username="u", is_active=True)


@app.post(
    "/users",
    response_model=UserPublic,
    status_code=201,
    tags=["users"],
    operation_id="create_user",
)
def create_user(user: UserCreate) -> UserPublic:
    return UserPublic(id=user.id, username=user.username, is_active=True)


@app.get(
    "/me",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me",
)
def read_me(token: str = Depends(oauth2_password)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/basic",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_basic",
)
def read_me_basic(creds=Depends(http_basic)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/bearer",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_bearer",
)
def read_me_bearer(creds=Depends(http_bearer)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/digest",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_digest",
)
def read_me_digest(creds=Depends(http_digest)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/cookie",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_cookie",
)
def read_me_cookie(s: str = Depends(api_key_cookie)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/query",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_query_key",
)
def read_me_query_key(s: str = Depends(api_key_query)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/authcode",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_authcode",
)
def read_me_authcode(token: str = Depends(oauth2_authcode)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/multi",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_multi",
)
def read_me_multi(token: str = Depends(oauth2_multi)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


@app.get(
    "/me/openid",
    response_model=UserPublic,
    tags=["users", "auth"],
    operation_id="read_me_openid",
)
def read_me_openid(token: str = Depends(openid)) -> UserPublic:
    return UserPublic(id=1, username="me", is_active=True)


# ══════════════════════════════════════════════════════════════════════
# Search / query parameters
# ══════════════════════════════════════════════════════════════════════

@app.get("/search", tags=["search"], operation_id="search_basic")
def search_basic(
    q: str = Query(..., min_length=1, max_length=200, description="Query"),
    limit: int = Query(10, ge=1, le=100, description="Limit"),
    offset: int = Query(0, ge=0),
    sort: Literal["asc", "desc"] = Query("asc", description="Sort direction"),
    tags: List[str] = Query(default_factory=list, description="Tag filter"),
) -> Dict[str, Any]:
    return {"q": q, "results": []}


@app.get("/search/advanced", tags=["search"], operation_id="search_advanced")
def search_advanced(
    q: str = Query(..., min_length=1),
    color: Optional[Color] = Query(None, description="Filter color"),
    priority: Optional[Priority] = Query(None),
    severity: Optional[Severity] = Query(None),
    created_after: Optional[datetime] = Query(None, description="ISO datetime"),
    created_before: Optional[datetime] = Query(None),
    price_min: Optional[float] = Query(None, ge=0),
    price_max: Optional[float] = Query(None, ge=0),
    in_stock: bool = Query(True),
    ids: List[UUID] = Query(default_factory=list, description="IDs"),
) -> Dict[str, Any]:
    return {"q": q}


@app.get("/search/{category}", tags=["search"], operation_id="search_by_category")
def search_by_category(
    category: Color = Path(..., description="Category enum"),
    x_request_id: str = Header(..., convert_underscores=True),
    session: Optional[str] = Cookie(None),
) -> Dict[str, Any]:
    return {"category": category}


# ══════════════════════════════════════════════════════════════════════
# Forms & files
# ══════════════════════════════════════════════════════════════════════

@app.post("/login", tags=["forms"], operation_id="login_form")
def login_form(
    username: str = Form(..., min_length=3, max_length=50),
    password: str = Form(..., min_length=8),
    remember: bool = Form(False),
) -> Dict[str, str]:
    return {"token": "xyz"}


@app.post("/token", tags=["auth"], operation_id="token_form")
def token_form(form: OAuth2PasswordRequestForm = Depends()) -> Dict[str, str]:
    return {"access_token": "t", "token_type": "bearer"}


@app.post("/upload", tags=["files"], operation_id="upload_one")
def upload_one(file: UploadFile = File(..., description="Single file")) -> Dict[str, str]:
    return {"filename": file.filename or ""}


@app.post("/upload/many", tags=["files"], operation_id="upload_many")
def upload_many(
    files: List[UploadFile] = File(..., description="Multiple files")
) -> Dict[str, int]:
    return {"count": len(files)}


@app.post("/upload/mixed", tags=["files"], operation_id="upload_mixed")
def upload_mixed(
    title: str = Form(..., min_length=1, max_length=100),
    description: Optional[str] = Form(None, max_length=500),
    file: UploadFile = File(..., description="Attachment"),
    tags: List[str] = Form(default_factory=list),
) -> Dict[str, str]:
    return {"title": title}


@app.post("/body/embed", tags=["items"], operation_id="body_embed")
def body_embed(
    item: Item = Body(..., embed=True),
    user: User = Body(..., embed=True),
    notes: str = Body("", embed=True),
) -> Dict[str, str]:
    return {"ok": "1"}


# ══════════════════════════════════════════════════════════════════════
# Generic / recursive / discriminated
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/tree",
    response_model=Node,
    tags=["generic"],
    operation_id="get_tree",
)
def get_tree() -> Node:
    return Node(name="root")


@app.post(
    "/tree",
    response_model=Node,
    tags=["generic"],
    operation_id="post_tree",
)
def post_tree(node: Node) -> Node:
    return node


@app.get(
    "/forest",
    response_model=Forest,
    tags=["generic"],
    operation_id="get_forest",
)
def get_forest() -> Forest:
    return Forest(roots=[], total=0)


@app.get(
    "/pets",
    response_model=List[PetOwner],
    tags=["generic"],
    operation_id="list_pets",
)
def list_pets() -> List[PetOwner]:
    return []


@app.post(
    "/pets",
    response_model=PetOwner,
    tags=["generic"],
    operation_id="create_pet",
)
def create_pet(owner: PetOwner) -> PetOwner:
    return owner


@app.get(
    "/pets/cat",
    response_model=Cat,
    tags=["generic"],
    operation_id="get_cat",
)
def get_cat() -> Cat:
    return Cat()


@app.get(
    "/pets/dog",
    response_model=Dog,
    tags=["generic"],
    operation_id="get_dog",
)
def get_dog() -> Dog:
    return Dog()


@app.get(
    "/pets/bird",
    response_model=Bird,
    tags=["generic"],
    operation_id="get_bird",
)
def get_bird() -> Bird:
    return Bird()


@app.get(
    "/circle",
    response_model=Circle,
    tags=["generic"],
    operation_id="get_circle",
)
def get_circle() -> Circle:
    return Circle(radius=1.0)


@app.get(
    "/alias",
    response_model=AliasModel,
    tags=["generic"],
    operation_id="get_alias",
)
def get_alias() -> AliasModel:
    return AliasModel(userName="u", fullEmail="a@b.com")


@app.post(
    "/alias",
    response_model=AliasModel,
    tags=["generic"],
    operation_id="post_alias",
)
def post_alias(a: AliasModel) -> AliasModel:
    return a


@app.get(
    "/slider",
    response_model=Slider,
    tags=["generic"],
    operation_id="get_slider",
)
def get_slider() -> Slider:
    return Slider(value=50)


@app.get(
    "/artifact",
    response_model=Artifact,
    tags=["generic"],
    operation_id="get_artifact",
)
def get_artifact() -> Artifact:
    return Artifact(id=uuid4(), created=datetime.now())


@app.get(
    "/legacy",
    response_model=LegacyModel,
    tags=["generic"],
    deprecated=True,
    operation_id="get_legacy",
)
def get_legacy() -> LegacyModel:
    return LegacyModel(old_id=1)


@app.get(
    "/status",
    response_model=Status,
    tags=["generic"],
    operation_id="get_status",
)
def get_status() -> Status:
    return Status()


@app.get(
    "/company",
    response_model=Company,
    tags=["generic"],
    operation_id="get_company",
)
def get_company() -> Company:
    return Company(
        name="Acme",
        founded=2000,
        ceo=User(id=1, username="ceo"),
        headquarters=Address(street="1", city="SF", zipcode="94105"),
    )


# ══════════════════════════════════════════════════════════════════════
# Pages (generic Page[T])
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/page/items",
    response_model=Page[Item],
    tags=["generic"],
    operation_id="page_items",
)
def page_items() -> Page[Item]:
    return Page[Item](items=[], total=0)


@app.get(
    "/page/users",
    response_model=Page[UserPublic],
    tags=["generic"],
    operation_id="page_users",
)
def page_users() -> Page[UserPublic]:
    return Page[UserPublic](items=[], total=0)


@app.get(
    "/page/tags",
    response_model=Page[Tag],
    tags=["generic"],
    operation_id="page_tags",
)
def page_tags() -> Page[Tag]:
    return Page[Tag](items=[], total=0)


# ══════════════════════════════════════════════════════════════════════
# Special response shapes
# ══════════════════════════════════════════════════════════════════════

@app.get(
    "/raw/html",
    response_class=HTMLResponse,
    tags=["special"],
    operation_id="raw_html",
)
def raw_html() -> str:
    return "<h1>hi</h1>"


@app.get(
    "/raw/text",
    response_class=PlainTextResponse,
    tags=["special"],
    operation_id="raw_text",
)
def raw_text() -> str:
    return "hi"


@app.get(
    "/raw/json",
    response_class=JSONResponse,
    tags=["special"],
    operation_id="raw_json",
)
def raw_json() -> Dict[str, Any]:
    return {"ok": True}


@app.get(
    "/empty",
    status_code=204,
    tags=["special"],
    operation_id="empty_204",
)
def empty_204() -> None:
    return None


@app.get(
    "/multi-status",
    tags=["special"],
    operation_id="multi_status",
    response_model=ItemOut,
    responses={
        200: {"description": "OK"},
        202: {"description": "Accepted", "model": Status},
        301: {"description": "Moved"},
        400: {"description": "Bad", "model": APIError},
        401: {"description": "Unauthorized", "model": APIError},
        403: {"description": "Forbidden", "model": APIError},
        404: {"description": "Not found", "model": APIError},
        409: {"description": "Conflict", "model": APIError},
        429: {"description": "Rate limit", "model": APIError},
        500: {"description": "Server error", "model": APIError},
        502: {"description": "Bad gateway"},
        503: {"description": "Service unavail"},
    },
)
def multi_status() -> ItemOut:
    return ItemOut(id=uuid4(), name="x", price=1)


# ══════════════════════════════════════════════════════════════════════
# Admin (extra security / operation variations)
# ══════════════════════════════════════════════════════════════════════

@app.delete(
    "/admin/reset",
    status_code=204,
    tags=["admin"],
    operation_id="admin_reset",
)
def admin_reset(
    token: str = Depends(oauth2_password),
    key: str = Depends(api_key_header),
) -> None:
    return None


@app.post(
    "/admin/broadcast",
    tags=["admin"],
    operation_id="admin_broadcast",
    responses={
        200: {"description": "Sent"},
        401: {"description": "Unauthorized", "model": APIError},
        403: {"description": "Forbidden", "model": APIError},
        429: {"description": "Too many", "model": APIError},
    },
)
def admin_broadcast(
    message: str = Body(..., embed=True, min_length=1, max_length=1000),
    severity: Severity = Body(Severity.info, embed=True),
    token: str = Depends(oauth2_multi),
) -> Dict[str, str]:
    return {"ok": "1"}


# ══════════════════════════════════════════════════════════════════════
# APIRouter for nested prefix/tags
# ══════════════════════════════════════════════════════════════════════

v1 = APIRouter(prefix="/v1", tags=["v1"])


@v1.get("/items", response_model=List[Item], operation_id="v1_list_items")
def v1_list_items() -> List[Item]:
    return []


@v1.get("/items/{item_id}", response_model=Item, operation_id="v1_get_item")
def v1_get_item(item_id: UUID) -> Item:
    return Item(name="x", price=1)


@v1.post("/items", response_model=ItemOut, status_code=201, operation_id="v1_create_item")
def v1_create_item(item: Item) -> ItemOut:
    return ItemOut(id=uuid4(), name=item.name, price=item.price)


@v1.get("/users", response_model=List[UserPublic], operation_id="v1_list_users")
def v1_list_users() -> List[UserPublic]:
    return []


@v1.get("/users/{user_id}", response_model=UserPublic, operation_id="v1_get_user")
def v1_get_user(user_id: int = Path(..., ge=1)) -> UserPublic:
    return UserPublic(id=user_id, username="u", is_active=True)


app.include_router(v1)


v2 = APIRouter(prefix="/v2", tags=["v2"])


@v2.get("/tree", response_model=Node, operation_id="v2_get_tree")
def v2_get_tree() -> Node:
    return Node(name="root")


@v2.post("/tree", response_model=Node, operation_id="v2_post_tree")
def v2_post_tree(node: Node) -> Node:
    return node


@v2.get("/pet/{pet_type}", operation_id="v2_get_pet")
def v2_get_pet(pet_type: Literal["cat", "dog", "bird"]) -> Dict[str, str]:
    return {"pet": pet_type}


app.include_router(v2)


# ══════════════════════════════════════════════════════════════════════
# Root
# ══════════════════════════════════════════════════════════════════════

@app.get("/", operation_id="root_index")
def root_index() -> Dict[str, str]:
    return {"app": "deep_schema_r2"}


@app.get("/health", operation_id="health_check")
def health_check() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz", operation_id="healthz_check", include_in_schema=False)
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


