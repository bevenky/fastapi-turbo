"""ROUND 2 — Deep validation parity app for fastapi-rs.

Expands R1 with endpoints covering the FULL Pydantic v2 error matrix
at every input location (query/path/header/cookie/body scalar/body-model
/nested-list/nested-dict/nested-tuple/form/file/root/discriminated-union)
and at every nesting depth 1..5.

Each endpoint is designed to, when hit with a single specific invalid
request body, produce a deterministic Pydantic ValidationError the runner
can compare byte-for-byte (detail[].{type,loc,msg,input,ctx,url}) across
stock FastAPI and fastapi-rs.

Uses ONLY stock FastAPI imports. When the app is loaded under fastapi-rs,
the compat shim rewires these to fastapi_rs.
"""
from __future__ import annotations

import datetime as _dt
import enum
import uuid
from decimal import Decimal
from typing import (
    Annotated,
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Union,
)

from fastapi import (
    Body,
    Cookie,
    FastAPI,
    File,
    Form,
    Header,
    Path,
    Query,
    UploadFile,
)
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    FutureDate,
    FutureDatetime,
    HttpUrl,
    Json,
    NaiveDatetime,
    PastDate,
    PastDatetime,
    RootModel,
    SecretStr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    UUID1,
    UUID3,
    UUID4,
    UUID5,
    ValidationError,
    conint,
    conlist,
    constr,
    field_validator,
    model_validator,
)


app = FastAPI(title="deep-validation-parity-r2")


@app.get("/health")
def health():
    return {"ok": True}


# ─────────────────────────── Enums / Literals ───────────────────────────

class Color(str, enum.Enum):
    red = "red"
    green = "green"
    blue = "blue"


class IntLevel(int, enum.Enum):
    LOW = 1
    MID = 5
    HIGH = 10


# =============================================================================
# SECTION A : QUERY PARAM ERROR MATRIX
# =============================================================================

@app.get("/q/int")
def q_int(n: int):
    return {"n": n}


@app.get("/q/float")
def q_float(n: float):
    return {"n": n}


@app.get("/q/bool")
def q_bool(flag: bool):
    return {"flag": flag}


@app.get("/q/str")
def q_str(s: str = Query(...)):
    return {"s": s}


@app.get("/q/ge")
def q_ge(n: int = Query(..., ge=0)):
    return {"n": n}


@app.get("/q/gt")
def q_gt(n: int = Query(..., gt=0)):
    return {"n": n}


@app.get("/q/le")
def q_le(n: int = Query(..., le=100)):
    return {"n": n}


@app.get("/q/lt")
def q_lt(n: int = Query(..., lt=100)):
    return {"n": n}


@app.get("/q/ge-le")
def q_ge_le(n: int = Query(..., ge=0, le=100)):
    return {"n": n}


@app.get("/q/gt-lt")
def q_gt_lt(n: int = Query(..., gt=0, lt=100)):
    return {"n": n}


@app.get("/q/ge-float")
def q_ge_float(n: float = Query(..., ge=0.0)):
    return {"n": n}


@app.get("/q/le-float")
def q_le_float(n: float = Query(..., le=1.0)):
    return {"n": n}


@app.get("/q/min-length")
def q_minlen(s: str = Query(..., min_length=3)):
    return {"s": s}


@app.get("/q/max-length")
def q_maxlen(s: str = Query(..., max_length=5)):
    return {"s": s}


@app.get("/q/min-max-length")
def q_minmaxlen(s: str = Query(..., min_length=3, max_length=5)):
    return {"s": s}


@app.get("/q/pattern")
def q_pattern(code: str = Query(..., pattern=r"^[A-Z]{3}$")):
    return {"code": code}


@app.get("/q/pattern-digits")
def q_pattern_digits(code: str = Query(..., pattern=r"^\d{4}$")):
    return {"code": code}


@app.get("/q/enum-str")
def q_enum_str(c: Color):
    return {"c": getattr(c, "value", c)}


@app.get("/q/enum-int")
def q_enum_int(lvl: IntLevel):
    return {"lvl": getattr(lvl, "value", lvl)}


@app.get("/q/literal")
def q_literal(mode: Literal["fast", "slow"]):
    return {"mode": mode}


@app.get("/q/literal-int")
def q_literal_int(n: Literal[1, 2, 3]):
    return {"n": n}


@app.get("/q/list-int")
def q_list_int(ids: List[int] = Query(...)):
    return {"ids": ids}


@app.get("/q/list-int-optional")
def q_list_int_opt(ids: Optional[List[int]] = Query(None)):
    return {"ids": ids}


@app.get("/q/required")
def q_required(must: str = Query(...)):
    return {"must": must}


@app.get("/q/uuid")
def q_uuid(u: uuid.UUID):
    return {"u": str(u)}


@app.get("/q/uuid4")
def q_uuid4(u: UUID4 = Query(...)):
    return {"u": str(u)}


@app.get("/q/datetime")
def q_datetime(d: _dt.datetime):
    return {"d": d.isoformat() if hasattr(d, "isoformat") else str(d)}


@app.get("/q/date")
def q_date(d: _dt.date):
    return {"d": d.isoformat() if hasattr(d, "isoformat") else str(d)}


@app.get("/q/time")
def q_time(t: _dt.time):
    return {"t": t.isoformat() if hasattr(t, "isoformat") else str(t)}


@app.get("/q/timedelta")
def q_timedelta(td: _dt.timedelta):
    return {"td": td.total_seconds() if hasattr(td, "total_seconds") else td}


@app.get("/q/multiple-of")
def q_mul_of(n: int = Query(..., multiple_of=5)):
    return {"n": n}


@app.get("/q/decimal")
def q_decimal(n: Decimal):
    return {"n": str(n)}


@app.get("/q/strict-int")
def q_strict_int(n: StrictInt = Query(...)):
    return {"n": n}


@app.get("/q/bytes")
def q_bytes(b: bytes = Query(...)):
    return {"b": b.decode(errors="replace") if isinstance(b, (bytes, bytearray)) else str(b)}


@app.get("/q/httpurl")
def q_httpurl(u: HttpUrl = Query(...)):
    return {"u": str(u)}


# Multi-query with multiple bad params (ordering)
@app.get("/q/multi")
def q_multi(a: int, b: int, c: int):
    return {"a": a, "b": b, "c": c}


# =============================================================================
# SECTION B : PATH PARAM ERROR MATRIX
# =============================================================================

@app.get("/p/int/{n}")
def p_int(n: int):
    return {"n": n}


@app.get("/p/float/{n}")
def p_float(n: float):
    return {"n": n}


@app.get("/p/bool/{flag}")
def p_bool(flag: bool):
    return {"flag": flag}


@app.get("/p/ge/{n}")
def p_ge(n: int = Path(..., ge=0)):
    return {"n": n}


@app.get("/p/gt/{n}")
def p_gt(n: int = Path(..., gt=0)):
    return {"n": n}


@app.get("/p/le/{n}")
def p_le(n: int = Path(..., le=100)):
    return {"n": n}


@app.get("/p/lt/{n}")
def p_lt(n: int = Path(..., lt=100)):
    return {"n": n}


@app.get("/p/min-length/{s}")
def p_minlen(s: str = Path(..., min_length=3)):
    return {"s": s}


@app.get("/p/pattern/{code}")
def p_pattern(code: str = Path(..., pattern=r"^[A-Z]{3}$")):
    return {"code": code}


@app.get("/p/uuid/{u}")
def p_uuid(u: uuid.UUID):
    return {"u": str(u)}


@app.get("/p/datetime/{d}")
def p_datetime(d: _dt.datetime):
    return {"d": d.isoformat() if hasattr(d, "isoformat") else str(d)}


@app.get("/p/date/{d}")
def p_date(d: _dt.date):
    return {"d": d.isoformat() if hasattr(d, "isoformat") else str(d)}


@app.get("/p/enum/{c}")
def p_enum(c: Color):
    return {"c": getattr(c, "value", c)}


@app.get("/p/literal/{mode}")
def p_literal(mode: Literal["fast", "slow"]):
    return {"mode": mode}


@app.get("/p/decimal/{n}")
def p_decimal(n: Decimal):
    return {"n": str(n)}


@app.get("/p/multi/{a}/{b}/{c}")
def p_multi(a: int, b: int, c: int):
    return {"a": a, "b": b, "c": c}


# =============================================================================
# SECTION C : HEADER PARAM ERROR MATRIX
# =============================================================================

@app.get("/h/int")
def h_int(x_count: int = Header(...)):
    return {"x_count": x_count}


@app.get("/h/float")
def h_float(x_ratio: float = Header(...)):
    return {"x_ratio": x_ratio}


@app.get("/h/bool")
def h_bool(x_flag: bool = Header(...)):
    return {"x_flag": x_flag}


@app.get("/h/ge")
def h_ge(x_count: int = Header(..., ge=0)):
    return {"x_count": x_count}


@app.get("/h/pattern")
def h_pattern(x_code: str = Header(..., pattern=r"^[A-Z]{3}$")):
    return {"x_code": x_code}


@app.get("/h/uuid")
def h_uuid(x_trace_id: uuid.UUID = Header(...)):
    return {"x_trace_id": str(x_trace_id)}


@app.get("/h/enum")
def h_enum(x_color: Color = Header(...)):
    return {"x_color": getattr(x_color, "value", x_color)}


@app.get("/h/literal")
def h_literal(x_mode: Literal["fast", "slow"] = Header(...)):
    return {"x_mode": x_mode}


# =============================================================================
# SECTION D : COOKIE PARAM ERROR MATRIX
# =============================================================================

@app.get("/c/int")
def c_int(session_id: int = Cookie(...)):
    return {"session_id": session_id}


@app.get("/c/float")
def c_float(ratio: float = Cookie(...)):
    return {"ratio": ratio}


@app.get("/c/bool")
def c_bool(flag: bool = Cookie(...)):
    return {"flag": flag}


@app.get("/c/ge")
def c_ge(session_id: int = Cookie(..., ge=0)):
    return {"session_id": session_id}


@app.get("/c/pattern")
def c_pattern(token: str = Cookie(..., pattern=r"^[A-Z]{3}$")):
    return {"token": token}


@app.get("/c/uuid")
def c_uuid(session_uuid: uuid.UUID = Cookie(...)):
    return {"session_uuid": str(session_uuid)}


@app.get("/c/enum")
def c_enum(theme: Color = Cookie(...)):
    return {"theme": getattr(theme, "value", theme)}


# =============================================================================
# SECTION E : BODY PRIMITIVE ERROR MATRIX
# =============================================================================

class IntBody(BaseModel):
    n: int


class FloatBody(BaseModel):
    n: float


class BoolBody(BaseModel):
    flag: bool


class StrBody(BaseModel):
    s: str


class BytesBody(BaseModel):
    b: bytes


@app.post("/b/int")
def b_int(m: IntBody): return m


@app.post("/b/float")
def b_float(m: FloatBody): return m


@app.post("/b/bool")
def b_bool(m: BoolBody): return m


@app.post("/b/str")
def b_str(m: StrBody): return m


@app.post("/b/bytes")
def b_bytes(m: BytesBody): return m


# Body scalar (single Body(...) param, not inside a model)
@app.post("/b/scalar-int")
def b_scalar_int(n: int = Body(...)):
    return {"n": n}


@app.post("/b/scalar-str")
def b_scalar_str(s: str = Body(...)):
    return {"s": s}


@app.post("/b/scalar-embed-int")
def b_scalar_embed_int(n: int = Body(..., embed=True)):
    return {"n": n}


# =============================================================================
# SECTION F : BODY FIELD CONSTRAINT MATRIX
# =============================================================================

class GeModel(BaseModel):
    n: int = Field(ge=0)


class GtModel(BaseModel):
    n: int = Field(gt=0)


class LeModel(BaseModel):
    n: int = Field(le=100)


class LtModel(BaseModel):
    n: int = Field(lt=100)


class GeLeModel(BaseModel):
    n: int = Field(ge=0, le=100)


class GtLtModel(BaseModel):
    n: int = Field(gt=0, lt=100)


class GeLeFloatModel(BaseModel):
    n: float = Field(ge=0.0, le=1.0)


class MultipleOfModel(BaseModel):
    n: int = Field(multiple_of=5)


class MultipleOfFloatModel(BaseModel):
    n: float = Field(multiple_of=0.5)


class MinLenModel(BaseModel):
    s: str = Field(min_length=3)


class MaxLenModel(BaseModel):
    s: str = Field(max_length=5)


class MinMaxLenModel(BaseModel):
    s: str = Field(min_length=3, max_length=5)


class PatternModel(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}$")


class ComplexStringModel(BaseModel):
    s: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")


class BytesMinLenModel(BaseModel):
    b: bytes = Field(min_length=3)


class BytesMaxLenModel(BaseModel):
    b: bytes = Field(max_length=3)


class DecimalConstraintModel(BaseModel):
    n: Decimal = Field(max_digits=5, decimal_places=2)


class DecimalPlacesOnly(BaseModel):
    n: Decimal = Field(decimal_places=2)


class FiniteFloatModel(BaseModel):
    n: float = Field(allow_inf_nan=False)


@app.post("/b/ge")
def b_ge(m: GeModel): return m


@app.post("/b/gt")
def b_gt(m: GtModel): return m


@app.post("/b/le")
def b_le(m: LeModel): return m


@app.post("/b/lt")
def b_lt(m: LtModel): return m


@app.post("/b/ge-le")
def b_ge_le(m: GeLeModel): return m


@app.post("/b/gt-lt")
def b_gt_lt(m: GtLtModel): return m


@app.post("/b/ge-le-float")
def b_ge_le_float(m: GeLeFloatModel): return m


@app.post("/b/multiple-of")
def b_multiple_of(m: MultipleOfModel): return m


@app.post("/b/multiple-of-float")
def b_multiple_of_float(m: MultipleOfFloatModel): return m


@app.post("/b/min-length")
def b_minlen(m: MinLenModel): return m


@app.post("/b/max-length")
def b_maxlen(m: MaxLenModel): return m


@app.post("/b/min-max-length")
def b_minmaxlen(m: MinMaxLenModel): return m


@app.post("/b/pattern")
def b_pattern(m: PatternModel): return m


@app.post("/b/complex-string")
def b_complex_str(m: ComplexStringModel): return m


@app.post("/b/bytes-min-length")
def b_bytes_min(m: BytesMinLenModel): return m


@app.post("/b/bytes-max-length")
def b_bytes_max(m: BytesMaxLenModel): return m


@app.post("/b/decimal-constraint")
def b_decimal_constraint(m: DecimalConstraintModel): return m


@app.post("/b/decimal-places")
def b_decimal_places(m: DecimalPlacesOnly): return m


@app.post("/b/finite-float")
def b_finite_float(m: FiniteFloatModel): return m


# =============================================================================
# SECTION G : STRICT TYPES
# =============================================================================

class StrictIntModel(BaseModel):
    n: StrictInt


class StrictStrModel(BaseModel):
    s: StrictStr


class StrictBoolModel(BaseModel):
    b: StrictBool


class StrictFloatModel(BaseModel):
    n: StrictFloat


@app.post("/b/strict-int")
def b_strict_int(m: StrictIntModel): return m


@app.post("/b/strict-str")
def b_strict_str(m: StrictStrModel): return m


@app.post("/b/strict-bool")
def b_strict_bool(m: StrictBoolModel): return m


@app.post("/b/strict-float")
def b_strict_float(m: StrictFloatModel): return m


# =============================================================================
# SECTION H : REQUIRED / MISSING / OPTIONAL
# =============================================================================

class ReqModel(BaseModel):
    a: int
    b: str
    c: float


class OptionalModel(BaseModel):
    a: Optional[int] = None
    b: Optional[str] = None


class OptNoneModel(BaseModel):
    a: Optional[int]  # required BUT allows None


@app.post("/b/required")
def b_required(m: ReqModel): return m


@app.post("/b/optional")
def b_optional(m: OptionalModel): return m


@app.post("/b/opt-none")
def b_opt_none(m: OptNoneModel): return m


# =============================================================================
# SECTION I : EXTRA FIELDS
# =============================================================================

class ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int


class AllowModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: int


class IgnoreModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    a: int


class ForbidNestedOuter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: "ForbidNestedInner"


class ForbidNestedInner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int


@app.post("/b/forbid-extra")
def b_forbid(m: ForbidModel): return m


@app.post("/b/allow-extra")
def b_allow(m: AllowModel): return m


@app.post("/b/ignore-extra")
def b_ignore(m: IgnoreModel): return m


@app.post("/b/forbid-nested")
def b_forbid_nested(m: ForbidNestedOuter): return m


ForbidNestedOuter.model_rebuild()


# =============================================================================
# SECTION J : COLLECTION TYPES
# =============================================================================

class ListIntModel(BaseModel):
    xs: List[int]


class ListStrModel(BaseModel):
    xs: List[str]


class ListFloatModel(BaseModel):
    xs: List[float]


class DictStrIntModel(BaseModel):
    d: Dict[str, int]


class DictStrStrModel(BaseModel):
    d: Dict[str, str]


class DictIntIntModel(BaseModel):
    d: Dict[int, int]


class TupleModel(BaseModel):
    t: Tuple[int, str, float]


class Tuple2Model(BaseModel):
    t: Tuple[int, int]


class TupleVariadicModel(BaseModel):
    t: Tuple[int, ...]


class SetIntModel(BaseModel):
    s: Set[int]


class FrozensetModel(BaseModel):
    s: FrozenSet[int]


class NestedListOfListModel(BaseModel):
    rows: List[List[int]]


class NestedListOfDictModel(BaseModel):
    items: List[Dict[str, int]]


class NestedDictOfListModel(BaseModel):
    groups: Dict[str, List[int]]


class NestedDictOfDictModel(BaseModel):
    m: Dict[str, Dict[str, int]]


class ListLenModel(BaseModel):
    xs: List[int] = Field(min_length=2, max_length=5)


class UniqueListModel(BaseModel):
    xs: Annotated[List[int], Field()]  # plain list
    # Using RootModel for unique
    # fallback: Set keeps uniqueness


@app.post("/b/list-int")
def b_list_int(m: ListIntModel): return m


@app.post("/b/list-str")
def b_list_str(m: ListStrModel): return m


@app.post("/b/list-float")
def b_list_float(m: ListFloatModel): return m


@app.post("/b/dict-str-int")
def b_dict_str_int(m: DictStrIntModel): return m


@app.post("/b/dict-str-str")
def b_dict_str_str(m: DictStrStrModel): return m


@app.post("/b/dict-int-int")
def b_dict_int_int(m: DictIntIntModel): return m


@app.post("/b/tuple")
def b_tuple(m: TupleModel): return m


@app.post("/b/tuple2")
def b_tuple2(m: Tuple2Model): return m


@app.post("/b/tuple-variadic")
def b_tuple_variadic(m: TupleVariadicModel): return m


@app.post("/b/set-int")
def b_set_int(m: SetIntModel): return m


@app.post("/b/frozenset")
def b_frozenset(m: FrozensetModel): return m


@app.post("/b/nested-list-ints")
def b_nested_list_ints(m: NestedListOfListModel): return m


@app.post("/b/list-of-dict")
def b_list_of_dict(m: NestedListOfDictModel): return m


@app.post("/b/dict-of-list")
def b_dict_of_list(m: NestedDictOfListModel): return m


@app.post("/b/dict-of-dict")
def b_dict_of_dict(m: NestedDictOfDictModel): return m


@app.post("/b/list-len")
def b_list_len(m: ListLenModel): return m


# =============================================================================
# SECTION K : UNIONS / DISCRIMINATED
# =============================================================================

class IntOrStr(BaseModel):
    x: Union[int, str]


class UnionThree(BaseModel):
    x: Union[int, str, float]


class Cat(BaseModel):
    kind: Literal["cat"]
    meow_volume: int


class Dog(BaseModel):
    kind: Literal["dog"]
    bark_loudness: int


class Pet(BaseModel):
    animal: Annotated[Union[Cat, Dog], Field(discriminator="kind")]


class UnionOfModels(BaseModel):
    entity: Union[Cat, Dog]


class NestedPetContainer(BaseModel):
    pets: List[Pet]


NestedPetContainer.model_rebuild()


@app.post("/b/union")
def b_union(m: IntOrStr): return m


@app.post("/b/union-three")
def b_union_three(m: UnionThree): return m


@app.post("/b/discriminated")
def b_discriminated(m: Pet): return m


@app.post("/b/union-of-models")
def b_union_of_models(m: UnionOfModels): return m


@app.post("/b/nested-pets")
def b_nested_pets(m: NestedPetContainer): return m


# =============================================================================
# SECTION L : ENUM / LITERAL IN BODY
# =============================================================================

class EnumBody(BaseModel):
    c: Color


class EnumIntBody(BaseModel):
    lvl: IntLevel


class LiteralBody(BaseModel):
    mode: Literal["fast", "slow"]


class LiteralIntBody(BaseModel):
    n: Literal[1, 2, 3]


class LiteralMixedBody(BaseModel):
    v: Literal[1, "a", True]


@app.post("/b/enum")
def b_enum(m: EnumBody): return m


@app.post("/b/enum-int")
def b_enum_int(m: EnumIntBody): return m


@app.post("/b/literal")
def b_literal(m: LiteralBody): return m


@app.post("/b/literal-int")
def b_literal_int(m: LiteralIntBody): return m


@app.post("/b/literal-mixed")
def b_literal_mixed(m: LiteralMixedBody): return m


# =============================================================================
# SECTION M : VALIDATORS
# =============================================================================

class FieldValidatorModel(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_must_be_lowercase(cls, v: str) -> str:
        if not v.islower():
            raise ValueError("name must be lowercase")
        return v


class FieldValidatorAssertModel(BaseModel):
    n: int

    @field_validator("n")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        assert v > 0, "n must be positive"
        return v


class ModelValidatorBeforeModel(BaseModel):
    a: int
    b: int

    @model_validator(mode="before")
    @classmethod
    def check_sum(cls, data):
        if isinstance(data, dict) and "a" in data and "b" in data:
            if data["a"] + data["b"] < 0:
                raise ValueError("sum must be non-negative")
        return data


class ModelValidatorAfterModel(BaseModel):
    a: int
    b: int

    @model_validator(mode="after")
    def check_a_lt_b(self):
        if self.a >= self.b:
            raise ValueError("a must be less than b")
        return self


def _must_be_even(v: int) -> int:
    if v % 2 != 0:
        raise ValueError("must be even")
    return v


def _strip_prefix(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("x-"):
        return v[2:]
    return v


class AfterValidatorModel(BaseModel):
    n: Annotated[int, AfterValidator(_must_be_even)]


class BeforeValidatorModel(BaseModel):
    name: Annotated[str, BeforeValidator(_strip_prefix)]


@app.post("/b/field-validator")
def b_field_validator(m: FieldValidatorModel): return m


@app.post("/b/field-validator-assert")
def b_field_validator_assert(m: FieldValidatorAssertModel): return m


@app.post("/b/model-validator-before")
def b_mv_before(m: ModelValidatorBeforeModel): return m


@app.post("/b/model-validator-after")
def b_mv_after(m: ModelValidatorAfterModel): return m


@app.post("/b/after-validator")
def b_after_val(m: AfterValidatorModel): return m


@app.post("/b/before-validator")
def b_before_val(m: BeforeValidatorModel): return m


# =============================================================================
# SECTION N : NESTING DEPTH MATRIX (1..5)
# =============================================================================

class Address(BaseModel):
    zip: int
    street: str


class User(BaseModel):
    name: str
    address: Address


class UsersWrapper(BaseModel):
    users: List[User]


class UsersDictWrapper(BaseModel):
    users: Dict[str, User]


class D1(BaseModel):
    v: int


class D2(BaseModel):
    a: D1


class D3(BaseModel):
    b: D2


class D4(BaseModel):
    c: D3


class D5(BaseModel):
    d: D4


D5.model_rebuild()


class DeepListD5(BaseModel):
    items: List[D5]


class DeepListInList(BaseModel):
    matrix: List[List[List[int]]]


class DeepMultiLevel(BaseModel):
    # List[Model(nested=List[Model])] with 3 intermediate indices
    groups: List["GroupOfGroups"]


class GroupOfGroups(BaseModel):
    inner_groups: List["InnerGroup"]


class InnerGroup(BaseModel):
    items: List[D1]


DeepMultiLevel.model_rebuild()


@app.post("/b/nested")
def b_nested(m: User): return m


@app.post("/b/nested-list")
def b_nested_list(m: UsersWrapper): return m


@app.post("/b/nested-dict")
def b_nested_dict(m: UsersDictWrapper): return m


@app.post("/b/depth-2")
def b_depth_2(m: D2): return m


@app.post("/b/depth-3")
def b_depth_3(m: D3): return m


@app.post("/b/depth-4")
def b_depth_4(m: D4): return m


@app.post("/b/depth-5")
def b_depth_5(m: D5): return m


@app.post("/b/deep-list-d5")
def b_deep_list_d5(m: DeepListD5): return m


@app.post("/b/deep-list-in-list")
def b_deep_list_in_list(m: DeepListInList): return m


@app.post("/b/deep-multi-level")
def b_deep_multi_level(m: DeepMultiLevel): return m


# =============================================================================
# SECTION O : SPECIALTY TYPES (uuid/datetime/date/time/timedelta/url/json/...)
# =============================================================================

class UuidModel(BaseModel):
    u: uuid.UUID


class Uuid1Model(BaseModel):
    u: UUID1


class Uuid3Model(BaseModel):
    u: UUID3


class Uuid4Model(BaseModel):
    u: UUID4


class Uuid5Model(BaseModel):
    u: UUID5


class DatetimeModel(BaseModel):
    d: _dt.datetime


class AwareDatetimeModel(BaseModel):
    d: AwareDatetime


class NaiveDatetimeModel(BaseModel):
    d: NaiveDatetime


class FutureDatetimeModel(BaseModel):
    d: FutureDatetime


class PastDatetimeModel(BaseModel):
    d: PastDatetime


class DateModel(BaseModel):
    d: _dt.date


class FutureDateModel(BaseModel):
    d: FutureDate


class PastDateModel(BaseModel):
    d: PastDate


class TimeModel(BaseModel):
    t: _dt.time


class TimedeltaModel(BaseModel):
    td: _dt.timedelta


class HttpUrlModel(BaseModel):
    u: HttpUrl


class HttpUrlMaxLen(BaseModel):
    u: HttpUrl = Field(max_length=20)


class SecretStrModel(BaseModel):
    pw: SecretStr


class JsonIntMapModel(BaseModel):
    data: Json[Dict[str, int]]


class JsonListModel(BaseModel):
    data: Json[List[int]]


@app.post("/b/uuid")
def b_uuid(m: UuidModel): return m


@app.post("/b/uuid1")
def b_uuid1(m: Uuid1Model): return m


@app.post("/b/uuid3")
def b_uuid3(m: Uuid3Model): return m


@app.post("/b/uuid4")
def b_uuid4(m: Uuid4Model): return m


@app.post("/b/uuid5")
def b_uuid5(m: Uuid5Model): return m


@app.post("/b/datetime")
def b_datetime(m: DatetimeModel): return m


@app.post("/b/aware-datetime")
def b_aware_datetime(m: AwareDatetimeModel): return m


@app.post("/b/naive-datetime")
def b_naive_datetime(m: NaiveDatetimeModel): return m


@app.post("/b/future-datetime")
def b_future_datetime(m: FutureDatetimeModel): return m


@app.post("/b/past-datetime")
def b_past_datetime(m: PastDatetimeModel): return m


@app.post("/b/date")
def b_date(m: DateModel): return m


@app.post("/b/future-date")
def b_future_date(m: FutureDateModel): return m


@app.post("/b/past-date")
def b_past_date(m: PastDateModel): return m


@app.post("/b/time")
def b_time(m: TimeModel): return m


@app.post("/b/timedelta")
def b_timedelta(m: TimedeltaModel): return m


@app.post("/b/httpurl")
def b_httpurl(m: HttpUrlModel): return m


@app.post("/b/httpurl-maxlen")
def b_httpurl_maxlen(m: HttpUrlMaxLen): return m


@app.post("/b/secret")
def b_secret(m: SecretStrModel): return m


@app.post("/b/json")
def b_json(m: JsonIntMapModel): return m


@app.post("/b/json-list")
def b_json_list(m: JsonListModel): return m


# =============================================================================
# SECTION P : ALIASES
# =============================================================================

class AliasModel(BaseModel):
    item_name: str = Field(alias="itemName")


class ValidationAliasModel(BaseModel):
    count: int = Field(validation_alias="num")


class PopulateByNameModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


@app.post("/b/alias")
def b_alias(m: AliasModel): return m


@app.post("/b/validation-alias")
def b_val_alias(m: ValidationAliasModel): return m


@app.post("/b/populate-by-name")
def b_populate(m: PopulateByNameModel): return m


# =============================================================================
# SECTION Q : RECURSIVE / ROOT MODELS
# =============================================================================

class Tree(BaseModel):
    value: int
    children: Optional[List["Tree"]] = None


Tree.model_rebuild()


IntListRoot = RootModel[List[int]]
StrDictRoot = RootModel[Dict[str, int]]


@app.post("/b/tree")
def b_tree(m: Tree): return m


@app.post("/b/rootmodel")
def b_rootmodel(m: IntListRoot): return m


@app.post("/b/rootmodel-dict")
def b_rootmodel_dict(m: StrDictRoot): return m


# =============================================================================
# SECTION R : MULTI-BODY EMBED
# =============================================================================

@app.post("/b/multi-body")
def b_multi_body(a: IntBody = Body(...), b: StrBody = Body(...)):
    return {"a": a, "b": b}


@app.post("/b/multi-body-3")
def b_multi_body_3(
    a: IntBody = Body(...),
    b: StrBody = Body(...),
    c: FloatBody = Body(...),
):
    return {"a": a, "b": b, "c": c}


# =============================================================================
# SECTION S : FORM / FILE
# =============================================================================

@app.post("/f/form-int")
def f_form_int(n: int = Form(...)):
    return {"n": n}


@app.post("/f/form-pattern")
def f_form_pattern(code: str = Form(..., pattern=r"^[A-Z]{3}$")):
    return {"code": code}


@app.post("/f/form-required")
def f_form_required(a: str = Form(...), b: str = Form(...)):
    return {"a": a, "b": b}


@app.post("/f/file-required")
async def f_file_required(upload: UploadFile = File(...)):
    content = await upload.read()
    return {"size": len(content), "name": upload.filename}


# =============================================================================
# SECTION T : MULTI-ERROR (ordering / count)
# =============================================================================

class MultiErrorModel(BaseModel):
    a: int
    b: int
    c: int


class MixedConstraintsModel(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    age: int = Field(ge=0, le=150)
    score: float = Field(ge=0.0, le=100.0)
    tags: List[str] = Field(min_length=1, max_length=10)


class ManyFieldsModel(BaseModel):
    a: int
    b: str
    c: float
    d: bool
    e: Color
    f: Literal["x", "y"]


@app.post("/b/multi-error")
def b_multi_error(m: MultiErrorModel): return m


@app.post("/b/mixed-constraints")
def b_mixed(m: MixedConstraintsModel): return m


@app.post("/b/many-fields")
def b_many(m: ManyFieldsModel): return m


# =============================================================================
# SECTION U : FROZEN / ANY / CALLABLE
# =============================================================================

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)
    a: int


class AnyTypeModel(BaseModel):
    v: Any


@app.post("/b/frozen")
def b_frozen(m: FrozenModel): return m


@app.post("/b/any")
def b_any(m: AnyTypeModel): return m


# =============================================================================
# SECTION V : REGEX / STRING EDGE CASES
# =============================================================================

class RegexComplexModel(BaseModel):
    token: str = Field(pattern=r"^[A-Z]{2,4}-\d{3,5}$")


class ConstrStringModel(BaseModel):
    s: constr(min_length=3, max_length=10, pattern=r"^[a-zA-Z0-9_]+$")


class ConintModel(BaseModel):
    n: conint(ge=1, le=10, multiple_of=2)


class ConlistModel(BaseModel):
    xs: conlist(int, min_length=2, max_length=4)


@app.post("/b/regex-complex")
def b_regex_complex(m: RegexComplexModel): return m


@app.post("/b/constr")
def b_constr(m: ConstrStringModel): return m


@app.post("/b/conint")
def b_conint(m: ConintModel): return m


@app.post("/b/conlist")
def b_conlist(m: ConlistModel): return m


# =============================================================================
# SECTION W : NESTED-OPTIONAL / DEFAULT / UNION-NONE
# =============================================================================

class NestedOptModel(BaseModel):
    child: Optional[Address] = None


class DefaultModel(BaseModel):
    n: int = 42
    s: str = "hello"


class UnionNoneModel(BaseModel):
    x: Union[int, None]  # == Optional[int] but required


@app.post("/b/nested-opt")
def b_nested_opt(m: NestedOptModel): return m


@app.post("/b/default")
def b_default(m: DefaultModel): return m


@app.post("/b/union-none")
def b_union_none(m: UnionNoneModel): return m


# =============================================================================
# SECTION X : SCIENTIFIC / EDGE FLOATS / NEG INT
# =============================================================================

class SciFloatModel(BaseModel):
    n: float


class NegIntModel(BaseModel):
    n: int = Field(lt=0)


@app.post("/b/sci-float")
def b_sci_float(m: SciFloatModel): return m


@app.post("/b/neg-int")
def b_neg_int(m: NegIntModel): return m


# =============================================================================
# SECTION Y : NON-JSON BODY (invalid JSON syntax)
# =============================================================================

# same endpoint used with invalid JSON body
@app.post("/b/json-any")
def b_json_any(m: Dict[str, Any] = Body(...)):
    return m


# =============================================================================
# SECTION Z : Dict of User (deep-dict parity)
# =============================================================================

@app.post("/b/dict-users")
def b_dict_users(m: UsersDictWrapper): return m


# All done.
