"""jsonable_encoder — re-exported from REAL fastapi.encoders.

Part of retiring the Python clone: the 259-line clone reimplementation was removed
in favor of real FastAPI's (identical public signature). This module is imported
during ``fastapi_turbo`` package init, BEFORE the compat shim installs, so
``fastapi.encoders`` resolves to the REAL package; the bound reference stays real
after the shim rebinds ``sys.modules``.
"""

from __future__ import annotations

from fastapi.encoders import jsonable_encoder

__all__ = ["jsonable_encoder"]
