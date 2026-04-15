"""Jinja2 template rendering for fastapi-rs.

Provides a ``Jinja2Templates`` class compatible with Starlette's interface.
Requires the ``jinja2`` package to be installed.
"""

from __future__ import annotations

from typing import Any


class Jinja2Templates:
    """Starlette-compatible Jinja2 template renderer.

    Usage::

        from fastapi_rs.templating import Jinja2Templates

        templates = Jinja2Templates(directory="templates")

        @app.get("/page")
        def page():
            return templates.TemplateResponse("index.html", {"request": request, "title": "Hi"})
    """

    def __init__(self, directory: str | None = None, **kwargs: Any):
        from jinja2 import Environment, FileSystemLoader

        self.directory = directory
        self.env = Environment(
            loader=FileSystemLoader(directory) if directory else None,
            **kwargs,
        )

    def get_template(self, name: str):
        """Return a Jinja2 Template object."""
        return self.env.get_template(name)

    def TemplateResponse(
        self,
        name: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
        **kwargs: Any,
    ):
        """Render a template and return an HTMLResponse.

        ``context`` should include a ``"request"`` key for Starlette
        compatibility, though it is not strictly required here.
        """
        from fastapi_rs.responses import HTMLResponse

        context = context or {}
        template = self.env.get_template(name)
        content = template.render(**context)
        return HTMLResponse(
            content=content,
            status_code=status_code,
            headers=headers,
            media_type=media_type or "text/html",
        )
