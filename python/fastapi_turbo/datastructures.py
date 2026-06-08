"""Data structures — re-exported from real starlette / fastapi.

The hand-written clone (URL/Headers/MutableHeaders/QueryParams/FormData/Address/
URLPath/Secret/State + Default/DefaultPlaceholder) reimplemented Starlette's own
datastructures. They're behaviorally identical to the real classes, and the real
ones are what flow through the door anyway (``_door_make_request`` builds a real
Starlette ``Request``, whose ``.url``/``.headers``/``.form()`` return real
datastructures). So this is a thin re-export following the responses.py /
requests.py / exceptions.py pattern.

``DefaultPlaceholder`` / ``Default`` come from real fastapi: a clone copy is an
active liability — ``isinstance(real_Default_instance, clone_DefaultPlaceholder)``
is False, so cross-boundary unwrap checks (a user / real-FastAPI-internal
``Default(fn)``) would silently miss. Re-exporting the real class fixes that.

Imported during package init BEFORE the compat shim rebinds ``sys.modules`` (via
applications.py), so these resolve to the REAL packages.

NOTE: real Starlette constructors are keyword-shaped where the old clone took
positional scope/raw tuples (``URL(scope=...)``, ``Headers(raw=[(bytes,bytes)])``,
``Address(host, port)``); door code that built them positionally (websockets.py)
was repointed to the real signatures.
"""
from __future__ import annotations

from starlette.datastructures import (
    Address,
    FormData,
    Headers,
    MutableHeaders,
    QueryParams,
    Secret,
    State,
    URL,
    URLPath,
)
from fastapi.datastructures import Default, DefaultPlaceholder

__all__ = [
    "URL",
    "URLPath",
    "Headers",
    "MutableHeaders",
    "QueryParams",
    "FormData",
    "Address",
    "Secret",
    "State",
    "Default",
    "DefaultPlaceholder",
]
