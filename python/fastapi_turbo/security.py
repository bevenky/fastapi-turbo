"""Security schemes for FastAPI compatibility.

These are callable classes that work as ``Depends()`` dependencies:

    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

    @app.get("/protected")
    async def protected(token: str = Depends(oauth2_scheme)):
        return {"token": token}
"""

# Note: do NOT use `from __future__ import annotations` — it converts
# type annotations to strings, breaking the DI system's ability to detect
# `request: Request` params for injection.

from typing import Annotated, Optional

from pydantic import BaseModel

from fastapi_turbo.exceptions import HTTPException
from fastapi_turbo.param_functions import Form as _Form
from fastapi_turbo.requests import Request

# Real FastAPI marks a ``Depends(scheme)`` as a SECURITY dependency (→ OpenAPI
# securitySchemes + the route's ``security`` requirement) only when the scheme is
# an instance of the real ``SecurityBase``. The scheme classes below subclass it
# so real ``get_dependant`` / ``get_openapi`` (the OpenAPI pivot + the adapter)
# recognize them — while KEEPING the clone's ``.model`` dict (real
# ``get_openapi`` serializes it via ``jsonable_encoder`` just the same, and the
# clone ``_openapi.py`` + ``scheme.model["type"]`` tests stay valid). Imported
# before the compat shim (``__init__.py``), so this resolves to REAL FastAPI.
from fastapi.security.base import SecurityBase as _SecurityBase

import inspect as _inspect

# Real FastAPI's get_dependant introspects a security scheme via
# inspect.signature(scheme). Our schemes' __call__ is (request, *args, **kwargs)
# for runtime flexibility, which leaks a phantom kwargs/args param into the pivot
# adapter (real FastAPI then mis-classifies it). Pin every scheme INSTANCE's
# introspected signature to a single ``request: Request`` param — the real schemes'
# shape — so the adapter sees a clean inject_request. The actual __call__ runtime
# is unchanged, and Request is now a real starlette subclass (the type bridge), so
# real get_dependant recognizes it.
_REQUEST_ONLY_SIGNATURE = _inspect.Signature(
    [_inspect.Parameter("request", _inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
)


def _request_sig(cls):
    """Class decorator: after ``__init__``, pin ``__signature__`` to ``(request: Request)``."""
    _orig_init = cls.__init__

    def __init__(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self.__signature__ = _REQUEST_ONLY_SIGNATURE

    __init__.__wrapped__ = _orig_init
    cls.__init__ = __init__
    return cls


# ── Credential models ──────────────────────────────────────────────


class HTTPBasicCredentials(BaseModel):
    """Holds username and password from HTTP Basic auth."""

    username: str
    password: str


class HTTPAuthorizationCredentials(BaseModel):
    """Holds scheme and credentials from an Authorization header."""

    scheme: str
    credentials: str


class OAuth2PasswordRequestForm:
    """Dependency class for OAuth2 password flow form data.

    Matches FastAPI's ``OAuth2PasswordRequestForm`` signature exactly —
    uses `Annotated[..., Form(...)]` so the generated OpenAPI `Body_*`
    component carries the `pattern: ^password$` on `grant_type`,
    `format: password` on `password`/`client_secret`, and the correct
    `required` fields.
    """

    def __init__(
        self,
        *,
        grant_type: Annotated[
            Optional[str],
            _Form(pattern="^password$"),
        ] = None,
        username: Annotated[str, _Form()],
        password: Annotated[str, _Form(json_schema_extra={"format": "password"})],
        scope: Annotated[str, _Form()] = "",
        client_id: Annotated[Optional[str], _Form()] = None,
        client_secret: Annotated[
            Optional[str],
            _Form(json_schema_extra={"format": "password"}),
        ] = None,
    ):
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split() if scope else []
        self.client_id = client_id
        self.client_secret = client_secret


class OAuth2PasswordRequestFormStrict(OAuth2PasswordRequestForm):
    """Like ``OAuth2PasswordRequestForm`` but requires
    ``grant_type="password"`` per the OAuth2 spec."""

    def __init__(
        self,
        *,
        grant_type: Annotated[str, _Form(pattern="^password$")],
        username: Annotated[str, _Form()],
        # Real FA's Strict form DROPS ``format: password`` (see
        # site-packages/fastapi/security/oauth2.py:256-258). Only the
        # non-Strict form adds it.
        password: Annotated[str, _Form()],
        scope: Annotated[str, _Form()] = "",
        client_id: Annotated[Optional[str], _Form()] = None,
        client_secret: Annotated[Optional[str], _Form()] = None,
    ):
        super().__init__(
            grant_type=grant_type,
            username=username,
            password=password,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
        )


# ── Helper: extract authorization header from request or string ────


def _get_authorization(request_or_str=None, **kwargs) -> str | None:
    """Extract the Authorization header value.

    Accepts either a Request object, a plain string (for backward
    compatibility with DI systems that pass extracted header values),
    or an ``authorization`` keyword argument.
    """
    # Check kwargs first (backward compat: scheme(authorization="Bearer ..."))
    if "authorization" in kwargs:
        return kwargs["authorization"]
    if request_or_str is None:
        return None
    if isinstance(request_or_str, str):
        return request_or_str
    # Request-like object
    if hasattr(request_or_str, "headers"):
        return request_or_str.headers.get("authorization")
    return None


# ── OAuth2 base class ────────────────────────────────────────────


@_request_sig
class OAuth2(_SecurityBase):
    """Base OAuth2 security scheme (matches FastAPI's OAuth2 base class).

    Can be used directly with custom flows or subclassed for specific
    OAuth2 flow types.
    """

    def __init__(
        self,
        *,
        flows: dict | None = None,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.model = {"type": "oauth2", "flows": flows or {}}
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        authorization = _get_authorization(request, **kwargs)
        if authorization:
            return authorization
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


# ── Security schemes ───────────────────────────────────────────────


@_request_sig
class OAuth2PasswordBearer(_SecurityBase):
    """OAuth2 password bearer scheme.

    Extracts the bearer token from the Authorization header.
    Usable as a Depends() callable.
    """

    def __init__(
        self,
        tokenUrl: str,
        scheme_name: str | None = None,
        scopes: dict[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.tokenUrl = tokenUrl
        self.scheme_name = scheme_name or self.__class__.__name__
        self.scopes = scopes or {}
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "oauth2",
            "flows": {
                "password": {
                    "tokenUrl": tokenUrl,
                    "scopes": self.scopes,
                }
            },
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        authorization = _get_authorization(request, **kwargs)
        if authorization and authorization.startswith("Bearer "):
            return authorization[7:]
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


@_request_sig
class HTTPBase(_SecurityBase):
    """Base class for HTTP-scheme security dependencies (``HTTPBearer``,
    ``HTTPDigest``, ``HTTPBasic``). FastAPI exports this as
    ``fastapi.security.http.HTTPBase`` and third-party auth libraries
    subclass it to implement custom schemes.
    """

    def __init__(
        self,
        *,
        scheme: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model: dict = {"type": "http", "scheme": scheme}
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs):
        """Generic HTTP scheme callable — returns the raw Authorization
        header as ``HTTPAuthorizationCredentials``. Subclasses override
        for scheme-specific parsing (Bearer, Basic, Digest).

        Missing header → 401 with ``WWW-Authenticate`` set to the
        scheme name (``Other`` / ``Bearer`` / ...); FA does the same.
        """
        authorization = _get_authorization(request, **kwargs)
        if authorization:
            # Split on whitespace — collapse runs so
            # ``"Other  foobar "`` becomes ``("Other", "foobar")``.
            parts = authorization.strip().split(None, 1)
            scheme = parts[0] if parts else ""
            credentials = parts[1].strip() if len(parts) > 1 else ""
            return HTTPAuthorizationCredentials(
                scheme=scheme,
                credentials=credentials,
            )
        if self.auto_error:
            scheme_name = self.model.get("scheme", "Bearer") if isinstance(
                getattr(self, "model", None), dict
            ) else "Bearer"
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": scheme_name.title()},
            )
        return None


@_request_sig
class HTTPBearer(HTTPBase):
    """HTTP Bearer scheme.

    Returns ``HTTPAuthorizationCredentials`` from the Authorization header.
    """

    def __init__(
        self,
        *,
        bearerFormat: str | None = None,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        super().__init__(
            scheme="bearer",
            scheme_name=scheme_name,
            description=description,
            auto_error=auto_error,
        )
        self.bearerFormat = bearerFormat
        if bearerFormat:
            self.model["bearerFormat"] = bearerFormat

    def make_not_authenticated_error(self) -> HTTPException:
        """FA parity: override hook for custom not-authenticated errors.

        The ``authentication_error_status_code`` tutorial subclasses
        HTTPBearer and overrides this method to return a 403 instead
        of the default 401.
        """
        return HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def __call__(self, request: Request = None, **kwargs) -> HTTPAuthorizationCredentials | None:
        authorization = _get_authorization(request, **kwargs)
        if authorization and authorization.startswith("Bearer "):
            return HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials=authorization[7:],
            )
        if self.auto_error:
            raise self.make_not_authenticated_error()
        return None


@_request_sig
class HTTPDigest(_SecurityBase):
    """HTTP Digest authentication scheme.

    Extracts a ``Digest`` Authorization header and returns
    ``HTTPAuthorizationCredentials``.  This matches FastAPI's behavior --
    it does NOT implement the full RFC 7616 challenge/response handshake.
    """

    def __init__(
        self,
        *,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "http",
            "scheme": "digest",
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> HTTPAuthorizationCredentials | None:
        authorization = _get_authorization(request, **kwargs)
        if authorization and authorization.lower().startswith("digest "):
            return HTTPAuthorizationCredentials(
                scheme="Digest",
                credentials=authorization[7:],
            )
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Digest"},
            )
        return None


@_request_sig
class HTTPBasic(_SecurityBase):
    """HTTP Basic authentication scheme."""

    def __init__(
        self,
        *,
        scheme_name: str | None = None,
        realm: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.scheme_name = scheme_name or self.__class__.__name__
        self.realm = realm
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "http",
            "scheme": "basic",
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> HTTPBasicCredentials | None:
        import base64

        authorization = _get_authorization(request, **kwargs)
        realm = f'realm="{self.realm}"' if self.realm else None
        www_auth = f"Basic {realm}" if realm else "Basic"

        def _not_auth() -> HTTPException:
            return HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": www_auth},
            )

        # FA parity: missing or non-basic scheme honours auto_error.
        # Malformed base64 / missing colon ALWAYS raise 401 (auto_error
        # only covers the "no header / wrong scheme" branch).
        if not authorization or not authorization.startswith("Basic "):
            if self.auto_error:
                raise _not_auth()
            return None
        try:
            decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        except Exception as e:
            raise _not_auth() from e
        username, sep, password = decoded.partition(":")
        if not sep:
            raise _not_auth()
        return HTTPBasicCredentials(username=username, password=password)


@_request_sig
class _APIKeyBase(_SecurityBase):
    """API-key scheme base. Extracts the key from the request INTERNALLY (header /
    query / cookie by ``name``) — matching real FastAPI's ``APIKeyBase``, which is
    a ``SecurityBase`` that declares NO parameter (the key is documented under
    ``components.securitySchemes``, not the operation's ``parameters``). The
    previous clone declared an ``api_key`` Header/Query/Cookie marker param, which
    made real ``get_openapi`` emit a phantom 422 + validation components. The
    ``@_request_sig`` decorator pins the introspected signature to
    ``(request: Request)`` so real ``get_dependant`` injects the request only."""

    _location: str = "header"

    def __init__(self, *, name: str, scheme_name: str | None = None,
                 description: str | None = None, auto_error: bool = True):
        self.name = name
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {"type": "apiKey", "in": self._location, "name": name}
        if description:
            self.model["description"] = description

    def _extract(self, request, kwargs) -> str | None:
        # Backward compat: an explicit value handed in by a caller / legacy DI.
        if kwargs.get("api_key") is not None:
            return kwargs["api_key"]
        if request is None:
            return None
        if self._location == "header":
            return request.headers.get(self.name)
        if self._location == "query":
            return request.query_params.get(self.name)
        if self._location == "cookie":
            return request.cookies.get(self.name)
        return None

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        api_key = self._extract(request, kwargs)
        if api_key:
            return api_key
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "APIKey"},
            )
        return None


class APIKeyHeader(_APIKeyBase):
    """API key from a request header."""
    _location = "header"


class APIKeyQuery(_APIKeyBase):
    """API key from a query parameter."""
    _location = "query"


class APIKeyCookie(_APIKeyBase):
    """API key from a cookie."""
    _location = "cookie"


class SecurityScopes:
    """Holds the scopes required by a security dependency."""

    def __init__(self, scopes: list[str] | None = None):
        self.scopes = scopes or []
        self.scope_str = " ".join(self.scopes)


# ── OAuth2 additional flows ─────────────────────────────────────────


@_request_sig
class OAuth2ClientCredentials(_SecurityBase):
    """OAuth2 client-credentials flow (server-to-server auth).

    The OAuth2 flow where a client authenticates with its own credentials
    (no user) to get an access token. Common for microservice-to-microservice
    auth and background job authentication.

    Usage::

        oauth2 = OAuth2ClientCredentials(tokenUrl="/token")

        @app.get("/svc")
        async def svc(token: str = Depends(oauth2)):
            ...
    """

    def __init__(
        self,
        tokenUrl: str,
        scheme_name: str | None = None,
        scopes: dict[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.tokenUrl = tokenUrl
        self.scheme_name = scheme_name or self.__class__.__name__
        self.scopes = scopes or {}
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": tokenUrl,
                    "scopes": self.scopes,
                }
            },
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        authorization = _get_authorization(request, **kwargs)
        if authorization and authorization.startswith("Bearer "):
            return authorization[7:]
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


@_request_sig
class OAuth2AuthorizationCodeBearer(_SecurityBase):
    """OAuth2 authorization-code flow (user-delegated auth)."""

    def __init__(
        self,
        authorizationUrl: str,
        tokenUrl: str,
        refreshUrl: str | None = None,
        scheme_name: str | None = None,
        scopes: dict[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.authorizationUrl = authorizationUrl
        self.tokenUrl = tokenUrl
        self.refreshUrl = refreshUrl
        self.scheme_name = scheme_name or self.__class__.__name__
        self.scopes = scopes or {}
        self.description = description
        self.auto_error = auto_error
        flow: dict = {
            "authorizationUrl": authorizationUrl,
            "tokenUrl": tokenUrl,
            "scopes": self.scopes,
        }
        if refreshUrl:
            flow["refreshUrl"] = refreshUrl
        self.model = {
            "type": "oauth2",
            "flows": {"authorizationCode": flow},
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        authorization = _get_authorization(request, **kwargs)
        # Parse like upstream FastAPI via ``get_authorization_scheme_param``:
        # splits on the first space, strips the token, and case-insensitively
        # checks the scheme. This lets ``"Bearer  testtoken "`` (double space
        # + trailing space) resolve to ``"testtoken"``.
        scheme = ""
        param = ""
        if authorization:
            s, _, p = authorization.partition(" ")
            scheme = s
            param = p.strip()
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return param


# ── OpenID Connect ─────────────────────────────────────────────────


@_request_sig
class OpenIdConnect(_SecurityBase):
    """OpenID Connect discovery-URL based auth scheme."""

    def __init__(
        self,
        *,
        openIdConnectUrl: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.openIdConnectUrl = openIdConnectUrl
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "openIdConnect",
            "openIdConnectUrl": openIdConnectUrl,
        }
        if description:
            self.model["description"] = description

    async def __call__(self, request: Request = None, **kwargs) -> str | None:
        # FA parity: pass the raw Authorization header through unchanged
        # — including the ``Bearer `` prefix. Callers who want just the
        # credentials must strip it themselves.
        authorization = _get_authorization(request, **kwargs)
        if authorization:
            return authorization
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None
