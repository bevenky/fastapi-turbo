"""HTTP / WebSocket status codes — re-exported from real ``starlette.status``.

The hand-written clone constants were byte-identical to Starlette's (same 82
``HTTP_*`` / ``WS_*`` names, same values — verified), so this is a pure
re-export. We copy by ``dir()`` rather than ``import *`` because Starlette keeps
four legacy 4xx aliases (``HTTP_413_REQUEST_ENTITY_TOO_LARGE``,
``HTTP_414_REQUEST_URI_TOO_LONG``, ``HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE``,
``HTTP_422_UNPROCESSABLE_ENTITY``) as module attributes but excludes them from
``__all__``, so ``import *`` would drop them and break code/tests that use the
legacy names.

Accessing those four legacy aliases triggers a ``DeprecationWarning`` via
Starlette's module ``__getattr__``; the clone exposed them as plain constants
(no warning), so we bake the values under ``catch_warnings`` to preserve that
behavior — otherwise importing this module under ``filterwarnings=error`` (e.g.
the upstream FastAPI test suite) fails at import.

Imported at ``__init__.py`` before ``compat.install()``, so ``starlette.status``
resolves to the REAL package; the bound names stay real after the shim runs.
"""
import warnings as _warnings

from starlette import status as _status

with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", DeprecationWarning)
    globals().update({_n: getattr(_status, _n) for _n in dir(_status) if _n.isupper()})

__all__ = [_n for _n in dir(_status) if _n.isupper()]
