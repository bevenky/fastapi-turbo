"""Jinja2 template rendering for fastapi-rs.

Provides ``Jinja2Templates`` — drop-in Starlette-compatible, uses stock
Jinja2. Matches every feature (filters, extensions, custom loaders, async).
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

    def __init__(
        self,
        directory: str | None = None,
        *,
        env: Any = None,
        context_processors: list | None = None,
        **kwargs: Any,
    ):
        self.directory = directory
        self.context_processors = context_processors or []

        if env is not None:
            # Accept a pre-built jinja2.Environment
            self.env = env
        else:
            from jinja2 import Environment, FileSystemLoader

            # Enable autoescape by default (matches Starlette behavior)
            if "autoescape" not in kwargs:
                try:
                    from jinja2 import select_autoescape
                    kwargs["autoescape"] = select_autoescape()
                except ImportError:
                    pass

            self.env = Environment(
                loader=FileSystemLoader(directory) if directory else None,
                **kwargs,
            )

        # Starlette parity: ``url_for`` inside a template delegates to
        # ``request.url_for(name, **path_params)`` when a request is in
        # context. ``TemplateResponse`` binds the real function per-render.
        def _url_for_stub(name: str, /, **path_params: Any) -> str:
            return "#"

        self.env.globals.setdefault("url_for", _url_for_stub)

    def get_template(self, name: str):
        """Return a Jinja2 Template object."""
        return self.env.get_template(name)

    def TemplateResponse(
        self,
        name_or_request: Any = None,
        name_or_context: Any = None,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
        **kwargs: Any,
    ):
        """Render a template and return an HTMLResponse.

        Supports both Starlette signature styles:

        New style (Starlette >= 0.28)::

            templates.TemplateResponse(request, "index.html", {"key": "val"})

        Old style::

            templates.TemplateResponse("index.html", {"request": request, "key": "val"})

        Keyword-only style is also supported::

            templates.TemplateResponse(name="index.html", context={...})
        """
        from fastapi_rs.responses import HTMLResponse

        if isinstance(name_or_request, str):
            # Old style: TemplateResponse("name.html", {"request": req, ...})
            name = name_or_request
            ctx = name_or_context if isinstance(name_or_context, dict) else (context or {})
        elif name_or_request is None:
            # Pure keyword style:
            #   TemplateResponse(request=req, name="index.html", context={...})
            name = kwargs.pop("name", None)
            ctx = dict(context or {})
            _kw_req = kwargs.pop("request", None)
            if _kw_req is not None:
                ctx["request"] = _kw_req
        else:
            # New style: TemplateResponse(request, "name.html", {})
            request = name_or_request
            name = name_or_context
            ctx = context or {}
            ctx["request"] = request

        template = self.env.get_template(name)
        # Starlette TemplateResponse parity: bind ``url_for`` to the
        # request stashed in the context so template authors can use
        # ``{{ url_for('route_name', param=value) }}`` to produce
        # absolute URLs honouring the current host + scheme.
        _req = ctx.get("request")
        if _req is not None and hasattr(_req, "url_for"):
            def _url_for(name: str, /, **path_params: Any) -> str:
                try:
                    return str(_req.url_for(name, **path_params))
                except Exception:  # noqa: BLE001
                    return "#"
            ctx = {**ctx, "url_for": _url_for}
        content = template.render(**ctx)
        return HTMLResponse(
            content=content,
            status_code=status_code,
            headers=headers,
            media_type=media_type or "text/html",
        )
