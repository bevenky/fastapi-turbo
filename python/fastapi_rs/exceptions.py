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

    def __init__(self, errors=None, *, body=None, endpoint_ctx=None):
        self._errors = list(errors) if errors else []
        self.body = body
        self.endpoint_ctx = endpoint_ctx or {}
        ctx = self.endpoint_ctx
        self.endpoint_function = ctx.get("function")
        self.endpoint_path = ctx.get("path")
        self.endpoint_file = ctx.get("file")
        self.endpoint_line = ctx.get("line")
        super().__init__(self._errors)

    def errors(self):
        return self._errors

    def _format_endpoint_context(self) -> str:
        if not (self.endpoint_file and self.endpoint_line and self.endpoint_function):
            if self.endpoint_path:
                return f"\n  Endpoint: {self.endpoint_path}"
            return ""
        context = (
            f'\n  File "{self.endpoint_file}", line {self.endpoint_line}, '
            f'in {self.endpoint_function}'
        )
        if self.endpoint_path:
            context += f"\n    {self.endpoint_path}"
        return context

    def __str__(self) -> str:
        # FA format: "N validation error(s):\n  {err}\n..." + endpoint ctx.
        # Tests assert on both the count line and the trailing endpoint
        # context — skip context only if fully unavailable.
        if not self._errors and not self.endpoint_ctx:
            return super().__str__()
        count = len(self._errors)
        msg = f"{count} validation error{'s' if count != 1 else ''}:\n"
        for err in self._errors:
            msg += f"  {err}\n"
        msg += self._format_endpoint_context()
        return msg.rstrip()


class RequestValidationError(ValidationException):
    """Raised when request data fails validation.

    FastAPI inherits this from ``pydantic.ValidationError``, where ``errors``
    is a method that returns the error list. User handlers commonly write
    ``exc.errors()`` — so ``errors`` is exposed as a method here, not an
    attribute.
    """

    def __init__(self, errors, *, body=None, endpoint_ctx=None):
        super().__init__(errors, body=body, endpoint_ctx=endpoint_ctx)


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

    def __init__(self, errors, *, body=None, endpoint_ctx=None):
        super().__init__(errors, body=body, endpoint_ctx=endpoint_ctx)


class FastAPIError(RuntimeError):
    """Base class for FastAPI-specific errors raised by the framework
    (not HTTP errors). Matches ``fastapi.exceptions.FastAPIError``.
    """


class ResponseValidationError(ValidationException):
    """Raised when a response fails validation against a response_model.

    Matches ``fastapi.exceptions.ResponseValidationError``.
    """

    def __init__(self, errors, *, body=None, endpoint_ctx=None):
        super().__init__(errors, body=body, endpoint_ctx=endpoint_ctx)


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
