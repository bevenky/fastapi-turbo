"""Drift sensor for the real-FastAPI / pydantic internals the pivot relies on.

The "accelerate real FastAPI" pivot drives the Rust hot path off real FastAPI's
``route.dependant`` + pydantic-v2 ``ModelField``/``FieldInfo`` internals (which
are semi-private). The adapter (``_introspect_from_real_fastapi``) reads exactly
the attribute surface asserted below. Pinning it here means a breaking FastAPI or
pydantic bump fails **loudly, in one place** — instead of silently mis-extracting
params and serving wrong responses through the Rust door.

Runs in a subprocess with the compat shim disabled so it checks REAL fastapi.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = r'''
import fastapi, starlette
from fastapi import FastAPI, Depends, Header, Cookie
from fastapi.routing import APIRoute
from pydantic import BaseModel

# (1) FastAPI must remain a Starlette subclass — the pivot delegates the
# irreducible surface via super().__call__() == real Starlette's ASGI app.
assert issubclass(fastapi.FastAPI, starlette.applications.Starlette), \
    "FastAPI no longer subclasses Starlette"

class Item(BaseModel):
    name: str

class Out(BaseModel):
    name: str

def dep(x: str = "z"):
    return x

app = FastAPI()

from fastapi import Request, Response, BackgroundTasks, Query

@app.post("/u/{uid}", response_model=Out)
def h(uid: int, q: str = "z",
      h1: str = Header(default="hh"), c1: str = Cookie(default="cc"),
      item: Item | None = None, d: str = Depends(dep)):
    return {}

@app.get("/special")
def special(request: Request, response: Response, tasks: BackgroundTasks):
    return {}

route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/u/{uid}")
dep_ = route.dependant

# (2) Dependant exposes the param buckets + recursion the adapter walks.
for attr in ("path_params", "query_params", "header_params", "cookie_params",
             "body_params", "dependencies", "cache_key"):
    assert hasattr(dep_, attr), f"Dependant.{attr} missing"
assert dep_.path_params and dep_.query_params, "path/query params not populated"
assert dep_.dependencies and hasattr(dep_.dependencies[0], "call"), \
    "Dependant.dependencies is not recursive Dependant list"

# (2b) Dependant special-param slots the adapter maps to inject_* kinds.
sdep = next(r for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/special").dependant
for attr in ("request_param_name", "http_connection_param_name", "response_param_name",
             "background_tasks_param_name", "security_scopes_param_name",
             "websocket_param_name"):
    assert hasattr(sdep, attr), f"Dependant.{attr} missing"
assert sdep.request_param_name == "request", "request_param_name not populated"
assert sdep.response_param_name == "response", "response_param_name not populated"
assert sdep.background_tasks_param_name == "tasks", "background_tasks_param_name not populated"

# (2c) Dependant callable-classification properties the adapter reads to set
# is_async_dep / is_generator_dep (must handle callable instances, gens).
for attr in ("is_coroutine_callable", "is_gen_callable", "is_async_gen_callable"):
    assert hasattr(dep_, attr), f"Dependant.{attr} missing"
def _g_dep():
    yield 1
gdep_app = FastAPI()
@gdep_app.get("/g")
def gh(v=Depends(_g_dep)):
    return {}
gdep = next(r for r in gdep_app.routes
            if isinstance(r, APIRoute) and r.path == "/g").dependant.dependencies[0]
assert gdep.is_gen_callable and not gdep.is_coroutine_callable, \
    "is_gen_callable classification changed"

# (3) pydantic-v2 ModelField.field_info exposes what the adapter reads.
mf = dep_.path_params[0]
fi = mf.field_info
assert hasattr(mf, "name"), "ModelField.name missing"
assert hasattr(fi, "annotation"), "FieldInfo.annotation missing"
assert callable(getattr(fi, "is_required", None)), "FieldInfo.is_required not callable"
for attr in ("alias", "default"):
    assert hasattr(fi, attr), f"FieldInfo.{attr} missing"

# (3b) ModelField._type_adapter is the constraint-bearing validator the door calls.
qmf = next(r for r in app.routes if isinstance(r, APIRoute)
           and r.path == "/u/{uid}").dependant.query_params[0]
assert hasattr(qmf, "_type_adapter") and hasattr(qmf._type_adapter, "validate_python"), \
    "ModelField._type_adapter / validate_python missing"
# Field(metadata) carries constraints (Query(gt=...)) → drives validation.
assert hasattr(fi, "metadata"), "FieldInfo.metadata missing"

# (4) APIRoute.response_field + flags + ModelField.validate/serialize (response_model).
assert route.response_field is not None, "APIRoute.response_field missing for response_model"
for attr in ("response_model_include", "response_model_exclude", "response_model_by_alias",
             "response_model_exclude_unset", "response_model_exclude_defaults",
             "response_model_exclude_none"):
    assert hasattr(route, attr), f"APIRoute.{attr} missing"
rf = route.response_field
assert callable(getattr(rf, "validate", None)), "ModelField.validate not callable"
assert callable(getattr(rf, "serialize", None)), "ModelField.serialize not callable"
_val, _errs = rf.validate({"name": "x"}, {}, loc=("response",))
assert not _errs and rf.serialize(_val, by_alias=True) == {"name": "x"}, \
    "ModelField.validate/serialize core changed"

print("DRIFT_OK", fastapi.__version__, starlette.__version__)
'''


def test_fastapi_internals_drift():
    env = dict(os.environ)
    env["FASTAPI_TURBO_NO_SHIM"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "DRIFT_OK" in proc.stdout, (
        "real-FastAPI internals drift detected — the pivot adapter's assumptions "
        f"broke.\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
