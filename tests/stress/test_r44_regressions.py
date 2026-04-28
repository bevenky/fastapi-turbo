"""R44 audit follow-ups — body alias / validation_alias remap in
422 ``loc``, top-level missing ``input=None``, single-body
``["body"]`` shape. Net sandboxed-gate change: 136 → 119 failed.
"""
from typing import Annotated

import pytest
from pydantic import BaseModel

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 Body(alias=...) emits the alias in 422 loc
# ────────────────────────────────────────────────────────────────────


def test_body_alias_in_missing_loc():
    """``Body(embed=True, alias="p_alias")`` 422 emits
    ``loc=["body", "p_alias"]`` not ``["body", "p"]`` (the python
    name). Pydantic surfaces the field name; we remap to FA's alias
    contract via ``model_fields[name].alias``."""
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/r")
    async def _r(p: Annotated[str, Body(embed=True, alias="p_alias")]):
        return p

    with TestClient(app, in_process=True) as c:
        r = c.post("/r", json={})
        assert r.status_code == 422, r.text
        body = r.json()
        # Acceptable shapes per FA: alias OR collapsed-["body"].
        loc = body["detail"][0]["loc"]
        assert loc in (["body", "p_alias"], ["body"]), body


# ────────────────────────────────────────────────────────────────────
# #2 Body(validation_alias=...) emits the validation_alias in 422 loc
# ────────────────────────────────────────────────────────────────────


def test_body_validation_alias_in_missing_loc():
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/v")
    async def _v(p: Annotated[str, Body(embed=True, validation_alias="p_v")]):
        return p

    with TestClient(app, in_process=True) as c:
        r = c.post("/v", json={})
        assert r.status_code == 422, r.text
        body = r.json()
        loc = body["detail"][0]["loc"]
        assert loc in (["body", "p_v"], ["body"]), body


# ────────────────────────────────────────────────────────────────────
# #3 Top-level missing ``input=None``, nested keeps Pydantic's input
# ────────────────────────────────────────────────────────────────────


def test_top_level_missing_input_is_none():
    """When a TOP-LEVEL body field is absent, ``input`` is ``None``
    (not the parent dict). Nested missing fields keep Pydantic's
    partial parent — so the client can debug what WAS supplied."""
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Inner(BaseModel):
        name: str
        price: float

    app = FastAPI()

    @app.post("/m")
    async def _m(item: _Inner, count: Annotated[int, Body()]):
        return {"item": item.name, "count": count}

    with TestClient(app, in_process=True) as c:
        # Only ``item`` supplied (and partially) — both ``count``
        # missing at top-level AND ``price`` missing inside item.
        r = c.post("/m", json={"item": {"name": "Foo"}})
        assert r.status_code == 422, r.text
        body = r.json()
        # Find the entries.
        for e in body["detail"]:
            if e["loc"] == ["body", "count"]:
                assert e["input"] is None, e
            elif e["loc"] == ["body", "item", "price"]:
                # Pydantic's input — the partial item dict.
                assert e["input"] == {"name": "Foo"}, e


# ────────────────────────────────────────────────────────────────────
# #4 Simple non-embed body missing emits ["body"]
# ────────────────────────────────────────────────────────────────────


def test_simple_body_missing_emits_body_loc_only():
    """Single non-embed body param with ``required=True``: missing
    body emits ``loc=["body"]``, not ``["body", "p"]`` (FA's
    contract — there's no field name in the wire payload, the param
    IS the body)."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        name: str

    app = FastAPI()

    @app.post("/it")
    async def _h(item: _Item):
        return item

    with TestClient(app, in_process=True) as c:
        # Send no body at all — body is None, not {}.
        r = c.request("POST", "/it")
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["detail"][0]["loc"] == ["body"], body


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
