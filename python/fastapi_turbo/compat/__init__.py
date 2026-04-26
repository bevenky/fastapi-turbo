"""Compatibility shims that make ``from fastapi import ...`` resolve to fastapi-turbo.

Auto-installed when ``import fastapi_turbo`` runs (disable with ``FASTAPI_TURBO_NO_SHIM=1``).
"""

from __future__ import annotations

import sys
from typing import Any

_installed = False

# Pre-shim class references captured at install time. Some libraries
# (or user code) do ``from starlette.datastructures import UploadFile``
# BEFORE importing fastapi_turbo, holding a reference to the ORIGINAL
# Starlette class. After the shim runs, ``sys.modules[
# 'starlette.datastructures'].UploadFile`` is OUR shim — a check
# against that class won't recognise the user's pre-shim instance.
# We capture the original here at install time so consumers (e.g.
# ``FormData.close``) can recognise both: the shimmed UploadFile we
# hand out post-install AND the upstream class anyone holds from
# pre-install imports. The R24 fix that probed ``sys.modules``
# couldn't see the pre-shim class because by then the module had
# been replaced — this captures the reference *before* the swap.
PRESHIM_STARLETTE_UPLOADFILE: Any = None


def install() -> None:
    """Install fastapi.* and starlette.* shims into sys.modules."""
    global _installed, PRESHIM_STARLETTE_UPLOADFILE
    if _installed:
        return

    # Capture the original Starlette UploadFile (if Starlette was
    # imported before us) BEFORE we overwrite the module. This is
    # the only chance — once the swap happens the module attribute
    # points at our shim and the original is unrecoverable from
    # ``sys.modules``.
    starlette_ds = sys.modules.get("starlette.datastructures")
    if starlette_ds is not None:
        captured = getattr(starlette_ds, "UploadFile", None)
        if captured is not None:
            PRESHIM_STARLETTE_UPLOADFILE = captured

    from fastapi_turbo.compat.starlette_shim import MODULES as starlette_modules
    from fastapi_turbo.compat.fastapi_shim import MODULES as fastapi_modules

    sys.modules.update(starlette_modules)
    sys.modules.update(fastapi_modules)
    _installed = True


def uninstall() -> None:
    """Remove all shims from sys.modules."""
    global _installed

    from fastapi_turbo.compat.starlette_shim import MODULES as starlette_modules
    from fastapi_turbo.compat.fastapi_shim import MODULES as fastapi_modules

    for key in list(starlette_modules):
        sys.modules.pop(key, None)
    for key in list(fastapi_modules):
        sys.modules.pop(key, None)
    _installed = False
