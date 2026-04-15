"""CORS middleware — maps to tower_http::cors::CorsLayer on the Rust side."""


class CORSMiddleware:
    """FastAPI-compatible CORS middleware backed by Tower-HTTP CorsLayer."""

    _fastapi_rs_middleware_type = "cors"

    def __init__(
        self,
        app=None,
        *,
        allow_origins=(),
        allow_methods=("GET",),
        allow_headers=(),
        allow_credentials=False,
        allow_origin_regex=None,
        expose_headers=(),
        max_age=600,
    ):
        self.app = app
        self.allow_origins = list(allow_origins)
        self.allow_methods = list(allow_methods)
        self.allow_headers = list(allow_headers)
        self.allow_credentials = allow_credentials
        self.allow_origin_regex = allow_origin_regex
        self.expose_headers = list(expose_headers)
        self.max_age = max_age
