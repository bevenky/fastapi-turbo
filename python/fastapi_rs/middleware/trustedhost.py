"""Trusted Host middleware placeholder."""


class TrustedHostMiddleware:
    """FastAPI-compatible TrustedHost middleware (placeholder for future Tower layer)."""

    _fastapi_rs_middleware_type = "trustedhost"

    def __init__(self, app=None, *, allowed_hosts=None):
        self.app = app
        self.allowed_hosts = allowed_hosts or ["*"]
