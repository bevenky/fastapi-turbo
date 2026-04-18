"""Exception classes matching FastAPI's interface."""


class HTTPException(Exception):
    """HTTP exception that results in an error response."""

    def __init__(self, status_code: int, detail=None, headers=None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(detail)


class RequestValidationError(Exception):
    """Raised when request data fails validation.

    FastAPI inherits this from ``pydantic.ValidationError``, where ``errors``
    is a method that returns the error list. User handlers commonly write
    ``exc.errors()`` — so ``errors`` is exposed as a method here, not an
    attribute.
    """

    def __init__(self, errors, *, body=None):
        self._errors = list(errors) if errors is not None else []
        self.body = body
        super().__init__(self._errors)

    def errors(self):
        return self._errors


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


class WebSocketRequestValidationError(Exception):
    """Raised when WebSocket request data fails validation.

    Matches FastAPI's ``fastapi.exceptions.WebSocketRequestValidationError``.
    """

    def __init__(self, errors, *, body=None):
        self._errors = list(errors) if errors is not None else []
        self.body = body
        super().__init__(self._errors)

    def errors(self):
        return self._errors


class FastAPIError(RuntimeError):
    """Base class for FastAPI-specific errors raised by the framework
    (not HTTP errors). Matches ``fastapi.exceptions.FastAPIError``.
    """


class ResponseValidationError(ValueError):
    """Raised when a response fails validation against a response_model.

    Matches ``fastapi.exceptions.ResponseValidationError``.
    """

    def __init__(self, errors, *, body=None):
        self._errors = list(errors) if errors is not None else []
        self.body = body
        super().__init__(self._errors)

    def errors(self):
        return self._errors
