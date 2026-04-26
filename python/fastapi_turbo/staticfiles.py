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

        # Prevent directory traversal. ``startswith`` was unsafe — a
        # configured directory ``/srv/static`` would also match
        # ``/srv/static-secret/leak.txt`` (sibling prefix), letting
        # ``GET /static/../static-secret/leak.txt`` escape the mount
        # root. ``os.path.commonpath`` matches whole path components
        # so the only paths that resolve inside the directory are
        # genuine descendants.
        path = path.lstrip("/")
        full_path = os.path.realpath(os.path.join(self.directory, path))
        dir_real = os.path.realpath(self.directory)

        try:
            common = os.path.commonpath([full_path, dir_real])
        except ValueError:
            # Different drives on Windows; can't be inside the dir.
            return "", None
        if common != dir_real:
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

    async def __call__(self, scope: dict, receive, send) -> None:
        """ASGI entry-point so StaticFiles can be mounted directly via
        ``app.mount("/static", StaticFiles(directory=...))`` and served
        by the in-process / TestClient ASGI dispatcher (the Rust path
        uses Tower's ServeDir; this implementation is just for in-process
        / sandbox parity).

        Behaviour mirrors Starlette's StaticFiles for the common cases:
        ``GET <prefix>/<path>`` → 200 + file bytes when found, 404
        otherwise. Method check: only ``GET`` and ``HEAD`` are
        accepted; everything else returns 405.
        """
        if scope.get("type") != "http":
            return
        method = (scope.get("method") or "GET").upper()
        if method not in ("GET", "HEAD"):
            await send({
                "type": "http.response.start",
                "status": 405,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"allow", b"GET, HEAD"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # ``scope['path']`` is the path AFTER the mount prefix has been
        # stripped by the parent app's mount-dispatch.
        rel = scope.get("path", "/")
        full_path, media_type = self.lookup_path(rel)
        if not full_path:
            body = b"Not Found"
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        try:
            with open(full_path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            body = f"Error reading file: {e}".encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        ct = media_type or "application/octet-stream"
        if (
            ct.startswith("text/")
            or ct in ("application/javascript", "application/json")
        ) and "charset=" not in ct.lower():
            ct = f"{ct}; charset=utf-8"
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", ct.encode("latin-1")),
                (b"content-length", str(len(data)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b"" if method == "HEAD" else data,
        })
