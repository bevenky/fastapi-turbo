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

def dep(x: str = "z"):
    return x

app = FastAPI()

@app.post("/u/{uid}")
def h(uid: int, q: str = "z",
      h1: str = Header(default="hh"), c1: str = Cookie(default="cc"),
      item: Item | None = None, d: str = Depends(dep)):
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

# (3) pydantic-v2 ModelField.field_info exposes what the adapter reads.
mf = dep_.path_params[0]
fi = mf.field_info
assert hasattr(mf, "name"), "ModelField.name missing"
assert hasattr(fi, "annotation"), "FieldInfo.annotation missing"
assert callable(getattr(fi, "is_required", None)), "FieldInfo.is_required not callable"
for attr in ("alias", "default"):
    assert hasattr(fi, attr), f"FieldInfo.{attr} missing"

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
