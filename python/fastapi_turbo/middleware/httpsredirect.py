"""HTTPS Redirect middleware.

Redirects HTTP requests to HTTPS by replacing the scheme in the URL
and returning a 307 redirect response.

Note: Full ASGI-level interception requires Rust/Tower integration.
This implementation provides a Python-level ``redirect_url`` helper
that computes the HTTPS redirect target for a given URL.
"""

from __future__ import annotations

from typing import Any


class HTTPSRedirectMiddleware:
    """FastAPI-compatible HTTPS redirect middleware.

    When applied, HTTP requests should be redirected to HTTPS.
    Provides a ``redirect_url`` helper to compute the target URL.
    """

    _fastapi_turbo_middleware_type = "httpsredirect"

    def __init__(self, app: Any = None):
        self.app = app

    @staticmethod
    def redirect_url(url: str) -> str:
        """Return *url* with the scheme changed to ``https``.

        If the URL already uses HTTPS, returns it unchanged.
        """
        if url.startswith("http://"):
            return "https://" + url[len("http://"):]
        return url

    @staticmethod
    def should_redirect(scheme: str) -> bool:
        """Return True if the request scheme indicates a redirect is needed."""
        return scheme.lower() not in ("https", "wss")
