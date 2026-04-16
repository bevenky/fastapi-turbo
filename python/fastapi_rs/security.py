"""Security schemes for FastAPI compatibility.

These are callable classes that work as ``Depends()`` dependencies:

    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

    @app.get("/protected")
    async def protected(token: str = Depends(oauth2_scheme)):
        return {"token": token}
"""

from __future__ import annotations

from fastapi_rs.exceptions import HTTPException


# ── Credential models ──────────────────────────────────────────────


class HTTPBasicCredentials:
    """Holds username and password from HTTP Basic auth."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def __repr__(self) -> str:
        return f"HTTPBasicCredentials(username={self.username!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HTTPBasicCredentials):
            return self.username == other.username and self.password == other.password
        return NotImplemented


class HTTPAuthorizationCredentials:
    """Holds scheme and credentials from an Authorization header."""

    def __init__(self, scheme: str, credentials: str):
        self.scheme = scheme
        self.credentials = credentials

    def __repr__(self) -> str:
        return f"HTTPAuthorizationCredentials(scheme={self.scheme!r}, credentials={self.credentials!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HTTPAuthorizationCredentials):
            return self.scheme == other.scheme and self.credentials == other.credentials
        return NotImplemented


class OAuth2PasswordRequestForm:
    """Dependency class for OAuth2 password flow form data."""

    def __init__(
        self,
        *,
        grant_type: str | None = None,
        username: str = "",
        password: str = "",
        scope: str = "",
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split() if scope else []
        self.client_id = client_id
        self.client_secret = client_secret


# ── Security schemes ───────────────────────────────────────────────


class OAuth2PasswordBearer:
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> str | None:
        # When used as a Depends(), the DI system calls this.
        # Since we can't easily hook into the header extraction from here,
        # the authorization param needs to be extracted by the caller.
        # For now, we implement a simple token extraction.
        if authorization and authorization.startswith("Bearer "):
            return authorization[7:]
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


class HTTPBearer:
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
        self.bearerFormat = bearerFormat
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "http",
            "scheme": "bearer",
        }
        if bearerFormat:
            self.model["bearerFormat"] = bearerFormat

    async def __call__(self, authorization: str | None = None, **kwargs) -> HTTPAuthorizationCredentials | None:
        if authorization and authorization.startswith("Bearer "):
            return HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials=authorization[7:],
            )
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class HTTPDigest:
    """HTTP Digest authentication scheme.

    Extracts a ``Digest`` Authorization header and returns
    ``HTTPAuthorizationCredentials``.  This matches FastAPI's behavior —
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> HTTPAuthorizationCredentials | None:
        if authorization and authorization.lower().startswith("digest "):
            return HTTPAuthorizationCredentials(
                scheme="Digest",
                credentials=authorization[7:],
            )
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class HTTPBasic:
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> HTTPBasicCredentials | None:
        import base64

        if authorization and authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
                return HTTPBasicCredentials(username=username, password=password)
            except Exception:
                pass
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": f'Basic realm="{self.realm or ""}"'},
            )
        return None


class APIKeyHeader:
    """API key from a request header."""

    def __init__(
        self,
        *,
        name: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.name = name
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "apiKey",
            "in": "header",
            "name": name,
        }

    async def __call__(self, **kwargs) -> str | None:
        # The API key should be passed from the header with the configured name.
        # When used in the DI system, the caller extracts it.
        api_key = kwargs.get(self.name) or kwargs.get(self.name.lower())
        if api_key:
            return api_key
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class APIKeyQuery:
    """API key from a query parameter."""

    def __init__(
        self,
        *,
        name: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.name = name
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "apiKey",
            "in": "query",
            "name": name,
        }

    async def __call__(self, **kwargs) -> str | None:
        api_key = kwargs.get(self.name)
        if api_key:
            return api_key
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class APIKeyCookie:
    """API key from a cookie."""

    def __init__(
        self,
        *,
        name: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ):
        self.name = name
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description
        self.auto_error = auto_error
        self.model = {
            "type": "apiKey",
            "in": "cookie",
            "name": name,
        }

    async def __call__(self, **kwargs) -> str | None:
        api_key = kwargs.get(self.name)
        if api_key:
            return api_key
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class SecurityScopes:
    """Holds the scopes required by a security dependency."""

    def __init__(self, scopes: list[str] | None = None):
        self.scopes = scopes or []
        self.scope_str = " ".join(self.scopes)


# ── OAuth2 additional flows ─────────────────────────────────────────


class OAuth2ClientCredentials:
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> str | None:
        if authorization and authorization.startswith("Bearer "):
            return authorization[7:]
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


class OAuth2AuthorizationCodeBearer:
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> str | None:
        if authorization and authorization.startswith("Bearer "):
            return authorization[7:]
        if self.auto_error:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


# ── OpenID Connect ─────────────────────────────────────────────────


class OpenIdConnect:
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

    async def __call__(self, authorization: str | None = None, **kwargs) -> str | None:
        if authorization:
            # User presents an OIDC id_token (typically in Authorization: Bearer ...)
            if authorization.startswith("Bearer "):
                return authorization[7:]
            return authorization
        if self.auto_error:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return None
