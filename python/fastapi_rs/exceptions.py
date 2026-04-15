"""Exception classes matching FastAPI's interface."""


class HTTPException(Exception):
    """HTTP exception that results in an error response."""

    def __init__(self, status_code: int, detail=None, headers=None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(detail)


class RequestValidationError(Exception):
    """Raised when request data fails validation."""

    def __init__(self, errors, *, body=None):
        self.errors = errors
        self.body = body
        super().__init__(errors)


class WebSocketException(Exception):
    """WebSocket-specific exception."""

    def __init__(self, code: int = 1008, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(reason)
