"""Exception classes matching FastAPI's interface."""


class HTTPException(Exception):
    """HTTP exception that results in an error response."""

    def __init__(self, status_code: int, detail=None, headers=None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(detail)


class ValidationException(Exception):
    """Base for all validation errors. FastAPI code checks isinstance(exc, ValidationException)."""

    def __init__(self, errors=None, *, body=None):
        self._errors = list(errors) if errors else []
        self.body = body
        super().__init__(self._errors)

    def errors(self):
        return self._errors


class RequestValidationError(ValidationException):
    """Raised when request data fails validation.

    FastAPI inherits this from ``pydantic.ValidationError``, where ``errors``
    is a method that returns the error list. User handlers commonly write
    ``exc.errors()`` — so ``errors`` is exposed as a method here, not an
    attribute.
    """

    def __init__(self, errors, *, body=None, endpoint_ctx=None):
        self.endpoint_ctx = endpoint_ctx
        super().__init__(errors, body=body)


class WebSocketException(Exception):
    """WebSocket-specific exception."""

    def __init__(self, code: int = 1008, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(reason)


class WebSocketDisconnect(Exception):
    """Raised when a WebSocket connection is disconnected."""

    def __init__(self, code: int = 1000, reason: str | None = None):
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket disconnected with code {code}")


class WebSocketRequestValidationError(ValidationException):
    """Raised when WebSocket request data fails validation.

    Matches FastAPI's ``fastapi.exceptions.WebSocketRequestValidationError``.
    """

    def __init__(self, errors, *, body=None):
        super().__init__(errors, body=body)


class FastAPIError(RuntimeError):
    """Base class for FastAPI-specific errors raised by the framework
    (not HTTP errors). Matches ``fastapi.exceptions.FastAPIError``.
    """


class ResponseValidationError(ValidationException):
    """Raised when a response fails validation against a response_model.

    Matches ``fastapi.exceptions.ResponseValidationError``.
    """

    def __init__(self, errors, *, body=None):
        super().__init__(errors, body=body)


class DependencyScopeError(FastAPIError):
    """Raised when a dependency is used outside its allowed scope."""
    pass


class PydanticV1NotSupportedError(FastAPIError):
    """Raised when Pydantic v1 model is used with FastAPI features requiring v2."""
    pass


class FastAPIDeprecationWarning(DeprecationWarning):
    pass


# ── Error models for structured error responses ──────��──────────────
from pydantic import BaseModel


class RequestErrorModel(BaseModel):
    detail: list[dict] = []


class WebSocketErrorModel(BaseModel):
    detail: str = ""
