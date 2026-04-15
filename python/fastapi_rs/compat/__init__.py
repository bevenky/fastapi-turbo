"""Compatibility shims that make ``from fastapi import ...`` resolve to fastapi-rs.

Auto-installed when ``import fastapi_rs`` runs (disable with ``FASTAPI_RS_NO_SHIM=1``).
"""

from __future__ import annotations

import sys

_installed = False


def install() -> None:
    """Install fastapi.* and starlette.* shims into sys.modules."""
    global _installed
    if _installed:
        return

    from fastapi_rs.compat.starlette_shim import MODULES as starlette_modules
    from fastapi_rs.compat.fastapi_shim import MODULES as fastapi_modules

    sys.modules.update(starlette_modules)
    sys.modules.update(fastapi_modules)
    _installed = True


def uninstall() -> None:
    """Remove all shims from sys.modules."""
    global _installed

    from fastapi_rs.compat.starlette_shim import MODULES as starlette_modules
    from fastapi_rs.compat.fastapi_shim import MODULES as fastapi_modules

    for key in list(starlette_modules):
        sys.modules.pop(key, None)
    for key in list(fastapi_modules):
        sys.modules.pop(key, None)
    _installed = False
