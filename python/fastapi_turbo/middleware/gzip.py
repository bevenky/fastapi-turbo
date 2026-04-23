"""GZip compression middleware — maps to tower_http::compression::CompressionLayer."""


class GZipMiddleware:
    """FastAPI-compatible GZip middleware backed by Tower-HTTP CompressionLayer."""

    _fastapi_turbo_middleware_type = "gzip"

    def __init__(self, app=None, *, minimum_size=500):
        self.app = app
        self.minimum_size = minimum_size
