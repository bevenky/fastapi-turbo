"""R43 audit follow-ups — three more in-process dispatcher edges:
multi-extra-dep error accumulation, Header(validation_alias=...)
honoured, bare ``list`` / ``set`` annotations treated as sequence
params. Net sandboxed-gate change: 155 → 136 failed (-19, after
counting cascading test passes triggered by the same fixes).
"""
from typing import Annotated

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 multi-extra-dep validation errors accumulate in one 422
# ────────────────────────────────────────────────────────────────────


def test_multiple_extra_dep_missing_errors_accumulate():
    """When app/router/route-level ``Depends(...)`` extra deps each
    declare a missing required param, ALL of their errors surface in
    one 422 — matches FA. Earlier we raised on the first dep's miss.
    """
    from fastapi_turbo import Depends, FastAPI, Header, Query
    from fastapi_turbo.testclient import TestClient

    async def needs_token(token: Annotated[str, Query()]):
        return token

    async def needs_x_token(x_token: Annotated[str, Header()]):
        return x_token

    app = FastAPI(dependencies=[Depends(needs_token), Depends(needs_x_token)])

    @app.put("/items/{item_id}")
    async def _u(item_id: str):
        return {"id": item_id}

    with TestClient(app, in_process=True) as c:
        r = c.put("/items/foo")
        assert r.status_code == 422, r.text
        body = r.json()
        locs = {tuple(e["loc"]) for e in body["detail"] if e["type"] == "missing"}
        assert ("query", "token") in locs, body
        assert ("header", "x-token") in locs, body


# ────────────────────────────────────────────────────────────────────
# #2 Header(validation_alias=...) is honoured at extraction time
# ────────────────────────────────────────────────────────────────────


def test_header_validation_alias_used_for_extraction():
    """Earlier the dispatcher's header path computed the lookup name
    via a helper that only checked ``marker.alias`` and the dash-
    converted python name — ``validation_alias`` was ignored. FA
    precedence is ``validation_alias`` > ``alias`` > python-name."""
    from fastapi_turbo import FastAPI, Header
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/h")
    async def _h(p: Annotated[str, Header(validation_alias="p_val_alias")]):
        return {"p": p}

    with TestClient(app, in_process=True) as c:
        # Sending the validation_alias hits the param.
        r = c.get("/h", headers={"p_val_alias": "ok"})
        assert r.status_code == 200, r.text
        assert r.json() == {"p": "ok"}, r.json()
        # Missing header: 422 with loc=["header","p_val_alias"].
        r2 = c.get("/h")
        assert r2.status_code == 422, r2.text
        assert r2.json()["detail"][0]["loc"] == ["header", "p_val_alias"]


# ────────────────────────────────────────────────────────────────────
# #3 bare ``list`` / ``set`` treated as sequence (multi-value) param
# ────────────────────────────────────────────────────────────────────


def test_form_param_bare_list_collects_repeated_values():
    """``items: list = Form()`` (bare ``list``, no parametrization)
    should collect repeated form values into a list. The
    ``is_list_param`` check used to require ``list[X]`` and missed
    the bare-class case, so a 3-value submission failed validation
    (Pydantic ``list_type`` error) on the first wire value."""
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/f")
    async def _h(items: list = Form()):
        return items

    with TestClient(app, in_process=True) as c:
        r = c.post("/f", data={"items": ["a", "b", "c"]})
        assert r.status_code == 200, r.text
        assert r.json() == ["a", "b", "c"]


def test_query_param_bare_list_collects_repeated_values():
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/q")
    async def _h(items: list = Query()):
        return items

    with TestClient(app, in_process=True) as c:
        r = c.get("/q?items=a&items=b&items=c")
        assert r.status_code == 200, r.text
        assert r.json() == ["a", "b", "c"]


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
