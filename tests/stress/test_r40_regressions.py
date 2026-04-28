"""R40 audit follow-ups — in-process dispatcher parity bugs found
running upstream FastAPI 0.136.0's full suite under a sandboxed
environment that denies loopback bind (forces the ASGITransport
path). Net: -307 failures vs R39 watermark.

Five fixes:

1. ``Optional[list[X]] = None`` query/header params returned ``[]``
   instead of the user's explicit ``None`` default when the request
   omitted the param.
2. The form-urlencoded parser overwrote repeated keys; a
   ``list[str] = Form(...)`` param then only saw the LAST value of
   a multi-value submission.
3. Form / file 422 errors used ``loc=["form", x]`` / ``loc=["file",
   x]`` instead of FastAPI's ``loc=["body", x]``.
4. The FastAPI 0.115+ parameter-model expansion wired
   ``Annotated[MyModel, Query()]`` through synthesized
   ``pm_<var>__<field>`` extraction params + a builder dep, but the
   in-process dispatcher fell through to the generic
   ``_resolve_dep`` for the builder (introspecting its closure with
   no params) AND leaked the synthesized fields into the user
   handler call (``unexpected keyword argument 'pm_p__p'``).
5. Mass-fix corollary: all kwargs that aren't on the user's
   endpoint signature must be filtered before invoking it.
"""
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 Optional[list[str]] = None preserves None default
# ────────────────────────────────────────────────────────────────────


def test_optional_list_query_preserves_none_default():
    """Probe-confirmed: ``p: list[str] | None = Query(None)`` returned
    ``{"p": []}`` when the request omitted ``?p=...``. The dispatcher
    used ``list(default_val) if default_val is not None else []`` —
    treating ``None`` as "no default" and falling through to ``[]``.
    R40 honours the explicit ``None``.
    """
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/q")
    async def _q(p: Annotated[list[str] | None, Query()] = None):
        return {"p": p}

    with TestClient(app, in_process=True) as c:
        r = c.get("/q")
        assert r.status_code == 200, r.text
        assert r.json() == {"p": None}, r.json()


def test_optional_list_header_preserves_none_default():
    from fastapi_turbo import FastAPI, Header
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/h")
    async def _h(p: Annotated[list[str] | None, Header()] = None):
        return {"p": p}

    with TestClient(app, in_process=True) as c:
        r = c.get("/h")
        assert r.status_code == 200, r.text
        assert r.json() == {"p": None}, r.json()


# ────────────────────────────────────────────────────────────────────
# #2 form-urlencoded multi-value parsing
# ────────────────────────────────────────────────────────────────────


def test_form_urlencoded_repeated_keys_collected_to_list():
    """``parse_qsl`` returns each occurrence as a separate (k,v); the
    parser must accumulate repeated keys into a list. Without the
    fix ``client.post(path, data={"p": ["a", "b"]})`` only saw
    ``"b"``."""
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/f")
    async def _f(p: Annotated[list[str], Form()]):
        return {"p": p}

    with TestClient(app, in_process=True) as c:
        r = c.post("/f", data={"p": ["alpha", "beta"]})
        assert r.status_code == 200, r.text
        assert r.json() == {"p": ["alpha", "beta"]}, r.json()


# ────────────────────────────────────────────────────────────────────
# #3 form/file missing 422 uses ``body`` loc prefix
# ────────────────────────────────────────────────────────────────────


def test_form_missing_required_emits_body_loc_prefix():
    """FA classifies form/file errors under ``body`` in the 422
    detail. Earlier the in-process dispatcher used
    ``_missing(kind, alias)`` directly, producing
    ``loc=["form","p"]`` — failed upstream's ``IsOneOf(None, {})``
    snapshot for the body shape."""
    from fastapi_turbo import FastAPI, Form
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.post("/f")
    async def _f(p: Annotated[str, Form()]):
        return {"p": p}

    with TestClient(app, in_process=True) as c:
        r = c.post("/f")
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["detail"][0]["loc"] == ["body", "p"], body


# ────────────────────────────────────────────────────────────────────
# #4 parameter-model builder dispatch (Annotated[MyModel, Query()])
# ────────────────────────────────────────────────────────────────────


def test_parameter_model_query_builds_via_in_process_dispatcher():
    """FA 0.115+ flattens ``p: Annotated[MyModel, Query()]`` into N
    synthesized field-extraction params + a builder dep. The
    in-process dispatcher used to:

    1. Pass the builder through the generic ``_resolve_dep`` (which
       introspects the wrapper closure — no params — and drops the
       wired ``dep_input_map``), so the model arrived built from an
       empty supplied-dict.
    2. Leak the synthesized ``pm_<var>__<field>`` extraction kwargs
       into the user handler call, which then tripped
       ``TypeError: unexpected keyword argument 'pm_p__p'``.

    R40 special-cases ``_is_param_model_builder`` deps in the
    dispatcher loop AND filters kwargs to the endpoint's signature
    before invoking it.
    """
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient

    class _Model(BaseModel):
        p: list[str] | None = None

    app = FastAPI()

    @app.get("/m")
    async def _m(model: Annotated[_Model, Query()]):
        return {"p": model.p}

    with TestClient(app, in_process=True) as c:
        r = c.get("/m?p=alpha&p=beta")
        assert r.status_code == 200, r.text
        assert r.json() == {"p": ["alpha", "beta"]}, r.json()
        # Missing case → field default ``None`` survives.
        r2 = c.get("/m")
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"p": None}, r2.json()


def test_parameter_model_with_validation_alias():
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient

    class _Model(BaseModel):
        p: str | None = Field(None, validation_alias="p_val")

    app = FastAPI()

    @app.get("/v")
    async def _v(model: Annotated[_Model, Query()]):
        return {"p": model.p}

    with TestClient(app, in_process=True) as c:
        r = c.get("/v?p_val=hello")
        assert r.status_code == 200, r.text
        assert r.json() == {"p": "hello"}, r.json()
        # Sending the python field name (not the validation_alias)
        # must NOT match the model — alias-driven extraction.
        r2 = c.get("/v?p=ignored")
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"p": None}, r2.json()


# ────────────────────────────────────────────────────────────────────
# #5 endpoint-signature kwarg filter
# ────────────────────────────────────────────────────────────────────


def test_endpoint_signature_filter_drops_synthesized_kwargs():
    """Static check on the dispatcher: it must filter ``kwargs`` to
    the endpoint's signature before calling, so synthesized
    extraction kwargs and other introspect-only placeholders never
    leak into the user fn. The earlier code passed ``**kwargs``
    blindly."""
    import inspect

    from fastapi_turbo.applications import FastAPI as _FA
    src = inspect.getsource(_FA._asgi_dispatch_in_process)
    # Must filter kwargs by the endpoint's signature.
    assert "_ep_sig_local" in src, src
    assert "VAR_KEYWORD" in src, src


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
