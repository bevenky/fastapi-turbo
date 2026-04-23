"""Static file serving for fastapi-turbo.

Provides a ``StaticFiles`` class compatible with Starlette's interface.
When mounted via ``app.mount()``, it serves files from a local directory.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any


class StaticFiles:
    """Serve static files from a local directory.

    Compatible with ``starlette.staticfiles.StaticFiles``.

    Usage::

        from fastapi_turbo.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory="static"), name="static")
    """

    def __init__(
        self,
        *,
        directory: str | None = None,
        packages: list[str] | None = None,
        html: bool = False,
        check_dir: bool = True,
    ):
        self.directory = directory
        self.packages = packages
        self.html = html
        self.check_dir = check_dir

        if check_dir and directory and not os.path.isdir(directory):
            raise RuntimeError(f"Directory '{directory}' does not exist")

    def lookup_path(self, path: str) -> tuple[str, str | None]:
        """Resolve a request path to a filesystem path and its media type.

        Returns (full_path, media_type) or ("", None) if not found.
        """
        if self.directory is None:
            return "", None

        # Prevent directory traversal
        path = path.lstrip("/")
        full_path = os.path.realpath(os.path.join(self.directory, path))
        dir_real = os.path.realpath(self.directory)

        if not full_path.startswith(dir_real):
            return "", None

        if os.path.isfile(full_path):
            media_type, _ = mimetypes.guess_type(full_path)
            return full_path, media_type or "application/octet-stream"

        # html mode: try index.html
        if self.html:
            index_path = os.path.join(full_path, "index.html")
            if os.path.isfile(index_path):
                return index_path, "text/html"

        return "", None
