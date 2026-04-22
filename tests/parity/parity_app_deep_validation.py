"""Deep validation parity app: ~150 endpoints exercising Pydantic v2 validation edge cases.

Uses ONLY stock FastAPI imports. The compat shim maps these to fastapi-rs when
the app is started under fastapi-rs.

Every endpoint is designed to produce specific, reproducible Pydantic ValidationErrors
so we can compare response bodies field-by-field between stock FastAPI and fastapi-rs.
"""
from __future__ import annotations

import datetime as _dt
import enum
import uuid
from decimal import Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union

from fastapi import Body, Cookie, FastAPI, Header, Path, Query
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    Json,
    RootModel,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    UUID4,
    field_validator,
    model_validator,
)

app = FastAPI(title="deep-validation-parity")


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


# ─────────────────────────── Basic query param types ───────────────────────────

@app.get("/q/int")
def q_int(n: int):
    return {"n": n}


@app.get("/q/float")
def q_float(n: float):
    return {"n": n}


@app.get("/q/bool")
def q_bool(flag: bool):
    return {"flag": flag}


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


@app.get("/q/min-length")
def q_minlen(s: str = Query(..., min_length=3)):
    return {"s": s}


@app.get("/q/max-length")
def q_maxlen(s: str = Query(..., max_length=5)):
    return {"s": s}


@app.get("/q/pattern")
def q_pattern(code: str = Query(..., pattern=r"^[A-Z]{3}$")):
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


# ─────────────────────────── Path params ───────────────────────────

@app.get("/p/int/{n}")
def p_int(n: int):
    return {"n": n}


@app.get("/p/uuid/{u}")
def p_uuid(u: uuid.UUID):
    return {"u": str(u)}


@app.get("/p/ge/{n}")
def p_ge(n: int = Path(..., ge=0)):
    return {"n": n}


@app.get("/p/enum/{c}")
def p_enum(c: Color):
    return {"c": getattr(c, "value", c)}


# ─────────────────────────── Header / Cookie ───────────────────────────

@app.get("/h/int")
def h_int(x_count: int = Header(...)):
    return {"x_count": x_count}


@app.get("/c/int")
def c_int(session_id: int = Cookie(...)):
    return {"session_id": session_id}


# ─────────────────────────── Body models: primitives ───────────────────────────

class IntBody(BaseModel):
    n: int


class FloatBody(BaseModel):
    n: float


class BoolBody(BaseModel):
    flag: bool


class StrBody(BaseModel):
    s: str


@app.post("/b/int")
def b_int(m: IntBody):
    return m


@app.post("/b/float")
def b_float(m: FloatBody):
    return m


@app.post("/b/bool")
def b_bool(m: BoolBody):
    return m


@app.post("/b/str")
def b_str(m: StrBody):
    return m


# ─────────────────────────── Body models: Field constraints ───────────────────────────

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


class MultipleOfModel(BaseModel):
    n: int = Field(multiple_of=5)


class MinLenModel(BaseModel):
    s: str = Field(min_length=3)


class MaxLenModel(BaseModel):
    s: str = Field(max_length=5)


class MinMaxLenModel(BaseModel):
    s: str = Field(min_length=3, max_length=5)


class PatternModel(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}$")


class DecimalConstraintModel(BaseModel):
    n: Decimal = Field(max_digits=5, decimal_places=2)


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


@app.post("/b/multiple-of")
def b_multiple_of(m: MultipleOfModel): return m


@app.post("/b/min-length")
def b_minlen(m: MinLenModel): return m


@app.post("/b/max-length")
def b_maxlen(m: MaxLenModel): return m


@app.post("/b/min-max-length")
def b_minmaxlen(m: MinMaxLenModel): return m


@app.post("/b/pattern")
def b_pattern(m: PatternModel): return m


@app.post("/b/decimal-constraint")
def b_decimal_constraint(m: DecimalConstraintModel): return m


# ─────────────────────────── Strict types ───────────────────────────

class StrictIntModel(BaseModel):
    n: StrictInt


class StrictStrModel(BaseModel):
    s: StrictStr


class StrictBoolModel(BaseModel):
    b: StrictBool


@app.post("/b/strict-int")
def b_strict_int(m: StrictIntModel): return m


@app.post("/b/strict-str")
def b_strict_str(m: StrictStrModel): return m


@app.post("/b/strict-bool")
def b_strict_bool(m: StrictBoolModel): return m


# ─────────────────────────── Missing / Required ───────────────────────────

class ReqModel(BaseModel):
    a: int
    b: str
    c: float


@app.post("/b/required")
def b_required(m: ReqModel): return m


class OptionalModel(BaseModel):
    a: Optional[int] = None
    b: Optional[str] = None


@app.post("/b/optional")
def b_optional(m: OptionalModel): return m


# ─────────────────────────── Extra fields ───────────────────────────

class ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int


class AllowModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: int


class IgnoreModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    a: int


@app.post("/b/forbid-extra")
def b_forbid(m: ForbidModel): return m


@app.post("/b/allow-extra")
def b_allow(m: AllowModel): return m


@app.post("/b/ignore-extra")
def b_ignore(m: IgnoreModel): return m


# ─────────────────────────── Collections ───────────────────────────

class ListIntModel(BaseModel):
    xs: List[int]


class DictStrIntModel(BaseModel):
    d: Dict[str, int]


class TupleModel(BaseModel):
    t: Tuple[int, str, float]


class NestedListModel(BaseModel):
    rows: List[List[int]]


class ListItemsModel(BaseModel):
    items: List["SubItem"]


class SubItem(BaseModel):
    value: int


ListItemsModel.model_rebuild()


@app.post("/b/list-int")
def b_list_int(m: ListIntModel): return m


@app.post("/b/dict-str-int")
def b_dict_str_int(m: DictStrIntModel): return m


@app.post("/b/tuple")
def b_tuple(m: TupleModel): return m


@app.post("/b/nested-list-ints")
def b_nested_list(m: NestedListModel): return m


@app.post("/b/list-items")
def b_list_items(m: ListItemsModel): return m


# ─────────────────────────── Unions / Discriminated ───────────────────────────

class IntOrStr(BaseModel):
    x: Union[int, str]


@app.post("/b/union")
def b_union(m: IntOrStr): return m


class Cat(BaseModel):
    kind: Literal["cat"]
    meow_volume: int


class Dog(BaseModel):
    kind: Literal["dog"]
    bark_loudness: int


class Pet(BaseModel):
    animal: Annotated[Union[Cat, Dog], Field(discriminator="kind")]


@app.post("/b/discriminated")
def b_discriminated(m: Pet): return m


# ─────────────────────────── Enums / Literals in body ───────────────────────────

class EnumBody(BaseModel):
    c: Color


class LiteralBody(BaseModel):
    mode: Literal["fast", "slow"]


@app.post("/b/enum")
def b_enum(m: EnumBody): return m


@app.post("/b/literal")
def b_literal(m: LiteralBody): return m


# ─────────────────────────── Field validators / model validators ───────────────────────────

class FieldValidatorModel(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_must_be_lowercase(cls, v: str) -> str:
        if not v.islower():
            raise ValueError("name must be lowercase")
        return v


class ModelValidatorBeforeModel(BaseModel):
    a: int
    b: int

    @model_validator(mode="before")
    @classmethod
    def check_both_present(cls, data):
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


@app.post("/b/field-validator")
def b_field_validator(m: FieldValidatorModel): return m


@app.post("/b/model-validator-before")
def b_mv_before(m: ModelValidatorBeforeModel): return m


@app.post("/b/model-validator-after")
def b_mv_after(m: ModelValidatorAfterModel): return m


# ─────────────────────────── Nested ───────────────────────────

class Address(BaseModel):
    zip: int
    street: str


class User(BaseModel):
    name: str
    address: Address


class UsersWrapper(BaseModel):
    users: List[User]


@app.post("/b/nested")
def b_nested(m: User): return m


@app.post("/b/nested-list")
def b_nested_list_users(m: UsersWrapper): return m


# ─────────────────────────── Specialty types ───────────────────────────

class UuidModel(BaseModel):
    u: uuid.UUID


class Uuid4Model(BaseModel):
    u: UUID4


class DatetimeModel(BaseModel):
    d: _dt.datetime


class DateModel(BaseModel):
    d: _dt.date


class TimeModel(BaseModel):
    t: _dt.time


class TimedeltaModel(BaseModel):
    td: _dt.timedelta


class HttpUrlModel(BaseModel):
    u: HttpUrl


class SecretModel(BaseModel):
    pw: SecretStr


class JsonModel(BaseModel):
    data: Json[Dict[str, int]]


@app.post("/b/uuid")
def b_uuid(m: UuidModel): return m


@app.post("/b/uuid4")
def b_uuid4(m: Uuid4Model): return m


@app.post("/b/datetime")
def b_datetime(m: DatetimeModel): return m


@app.post("/b/date")
def b_date(m: DateModel): return m


@app.post("/b/time")
def b_time(m: TimeModel): return m


@app.post("/b/timedelta")
def b_timedelta(m: TimedeltaModel): return m


@app.post("/b/httpurl")
def b_httpurl(m: HttpUrlModel): return m


@app.post("/b/secret")
def b_secret(m: SecretModel): return m


@app.post("/b/json")
def b_json(m: JsonModel): return m


# ─────────────────────────── Aliases ───────────────────────────

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


# ─────────────────────────── AfterValidator / BeforeValidator ───────────────────────────

def _must_be_even(v: int) -> int:
    if v % 2 != 0:
        raise ValueError("must be even")
    return v


def _strip_prefix(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("x-"):
        return v[2:]
    return v


class AnnotatedValidatorModel(BaseModel):
    n: Annotated[int, AfterValidator(_must_be_even)]


class BeforeValidatorModel(BaseModel):
    name: Annotated[str, BeforeValidator(_strip_prefix)]


@app.post("/b/after-validator")
def b_after_val(m: AnnotatedValidatorModel): return m


@app.post("/b/before-validator")
def b_before_val(m: BeforeValidatorModel): return m


# ─────────────────────────── Multi-error ordering ───────────────────────────

class MultiErrorModel(BaseModel):
    a: int
    b: int
    c: int


@app.post("/b/multi-error")
def b_multi_error(m: MultiErrorModel): return m


# ─────────────────────────── Frozen / RootModel ───────────────────────────

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)
    a: int


IntListRoot = RootModel[List[int]]


@app.post("/b/frozen")
def b_frozen(m: FrozenModel): return m


@app.post("/b/rootmodel")
def b_rootmodel(m: IntListRoot): return m


# ─────────────────────────── Recursive / self-referential ───────────────────────────

class Tree(BaseModel):
    value: int
    children: Optional[List["Tree"]] = None


Tree.model_rebuild()


@app.post("/b/tree")
def b_tree(m: Tree): return m


# ─────────────────────────── Multiple body fields (embed) ───────────────────────────

@app.post("/b/multi-body")
def b_multi_body(a: IntBody = Body(...), b: StrBody = Body(...)):
    return {"a": a, "b": b}


# ─────────────────────────── Dict value validation ───────────────────────────

class DictUsers(BaseModel):
    users: Dict[str, User]


@app.post("/b/dict-users")
def b_dict_users(m: DictUsers): return m


# ─────────────────────────── Optional with None ───────────────────────────

class OptNoneModel(BaseModel):
    a: Optional[int]


@app.post("/b/opt-none")
def b_opt_none(m: OptNoneModel): return m


# ─────────────────────────── Bool JSON vs string ───────────────────────────

class BoolModel(BaseModel):
    flag: bool


@app.post("/b/bool-json")
def b_bool_json(m: BoolModel): return m


# ─────────────────────────── Additional edge cases ───────────────────────────

class ScientificFloatModel(BaseModel):
    n: float


@app.post("/b/sci-float")
def b_sci_float(m: ScientificFloatModel): return m


class NegIntModel(BaseModel):
    n: int = Field(lt=0)


@app.post("/b/neg-int")
def b_neg_int(m: NegIntModel): return m


class BytesModel(BaseModel):
    b: bytes


@app.post("/b/bytes")
def b_bytes(m: BytesModel): return m


class SetIntModel(BaseModel):
    s: set[int]


@app.post("/b/set-int")
def b_set_int(m: SetIntModel): return m


class FrozensetModel(BaseModel):
    s: frozenset[int]


@app.post("/b/frozenset")
def b_frozenset(m: FrozensetModel): return m


class ComplexStringModel(BaseModel):
    s: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")


@app.post("/b/complex-string")
def b_complex_str(m: ComplexStringModel): return m


class MultipleConstraintsFloat(BaseModel):
    n: float = Field(ge=0.0, le=1.0)


@app.post("/b/mc-float")
def b_mc_float(m: MultipleConstraintsFloat): return m


class DeepNestedModel(BaseModel):
    a: "DeepB"


class DeepB(BaseModel):
    b: "DeepC"


class DeepC(BaseModel):
    c: "DeepD"


class DeepD(BaseModel):
    value: int


DeepNestedModel.model_rebuild()


@app.post("/b/deep-nested")
def b_deep_nested(m: DeepNestedModel): return m


class ListOfDictModel(BaseModel):
    items: List[Dict[str, int]]


@app.post("/b/list-of-dict")
def b_list_of_dict(m: ListOfDictModel): return m


class DictOfListModel(BaseModel):
    groups: Dict[str, List[int]]


@app.post("/b/dict-of-list")
def b_dict_of_list(m: DictOfListModel): return m


class MixedConstraintsModel(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    age: int = Field(ge=0, le=150)
    score: float = Field(ge=0.0, le=100.0)
    tags: List[str] = Field(min_length=1, max_length=10)


@app.post("/b/mixed-constraints")
def b_mixed(m: MixedConstraintsModel): return m


class UnionDiscriminatedDefault(BaseModel):
    x: Union[int, str, float]


@app.post("/b/union-three")
def b_union_three(m: UnionDiscriminatedDefault): return m


class ListLenModel(BaseModel):
    xs: List[int] = Field(min_length=2, max_length=5)


@app.post("/b/list-len")
def b_list_len(m: ListLenModel): return m


class ForbidAndNested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: "NestedInner"


class NestedInner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int


ForbidAndNested.model_rebuild()


@app.post("/b/forbid-nested")
def b_forbid_nested(m: ForbidAndNested): return m


class AnyTypeModel(BaseModel):
    v: Any


@app.post("/b/any")
def b_any(m: AnyTypeModel): return m


class RegexComplexModel(BaseModel):
    token: str = Field(pattern=r"^[A-Z]{2,4}-\d{3,5}$")


@app.post("/b/regex-complex")
def b_regex_complex(m: RegexComplexModel): return m


class NestedOptModel(BaseModel):
    child: Optional[Address] = None


@app.post("/b/nested-opt")
def b_nested_opt(m: NestedOptModel): return m


class UnionOfModels(BaseModel):
    entity: Union[Cat, Dog]


@app.post("/b/union-of-models")
def b_union_of_models(m: UnionOfModels): return m
