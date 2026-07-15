"""Security schemes — re-exported from REAL ``fastapi.security``.

The clone scheme classes (17 callables subclassing only the real
``SecurityBase`` while keeping a dict-shaped ``.model``) are retired. The dict
``.model`` existed for the clone ``_openapi.py`` (``isinstance(scheme.model,
dict)``), which has since been deleted — real ``fastapi.openapi.utils
.get_openapi`` runs now and serializes the real pydantic ``SecurityScheme``
models natively, so the real classes are the byte-parity source of truth for
``components.securitySchemes`` (the dict shape was itself a divergence source).

Runtime behavior comes from the real classes too: real ``get_dependant``
introspects the genuine ``__call__(self, request: Request)`` signatures (no
``__signature__`` pinning needed), and the door injects a real
starlette-subclass ``Request`` (see ``requests.py``) that the schemes read.

Imported during ``fastapi_turbo`` package init BEFORE the compat shim patches
the real packages, so ``fastapi.security`` resolves to the REAL package; the
bound references stay real after the shim runs.

``OAuth2ClientCredentials`` is a turbo EXTENSION — real FastAPI 0.138 ships no
client-credentials scheme. It stays here as a thin subclass of the real
``OAuth2`` carrying the ``clientCredentials`` flow, with bearer-token
extraction identical to ``OAuth2PasswordBearer``; the compat shim also patches
it onto ``fastapi.security`` so ``from fastapi.security import
OAuth2ClientCredentials`` keeps working.
"""

# Note: do NOT use `from __future__ import annotations` — it converts
# type annotations to strings, breaking the DI system's ability to detect
# `request: Request` params for injection.

from typing import Any, Optional, cast

from fastapi.openapi.models import OAuthFlows as _OAuthFlowsModel
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    OAuth2,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
    SecurityScopes,
)
from fastapi.security.base import SecurityBase
from fastapi.security.http import HTTPBase
from fastapi.security.utils import get_authorization_scheme_param as _get_scheme_param
from starlette.requests import Request

__all__ = [
    "APIKeyCookie",
    "APIKeyHeader",
    "APIKeyQuery",
    "HTTPAuthorizationCredentials",
    "HTTPBase",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "OAuth2",
    "OAuth2AuthorizationCodeBearer",
    "OAuth2ClientCredentials",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "OpenIdConnect",
    "SecurityBase",
    "SecurityScopes",
]


class OAuth2ClientCredentials(OAuth2):
    """OAuth2 client-credentials flow (server-to-server auth) — turbo extension.

    The OAuth2 flow where a client authenticates with its own credentials
    (no user) to get an access token. Common for microservice-to-microservice
    auth and background job authentication. Real FastAPI has no such scheme;
    this mirrors the real ``OAuth2PasswordBearer`` implementation with a
    ``clientCredentials`` flow instead of ``password``.

    Usage::

        oauth2 = OAuth2ClientCredentials(tokenUrl="/token")

        @app.get("/svc")
        async def svc(token: str = Depends(oauth2)):
            ...
    """

    def __init__(
        self,
        tokenUrl: str,
        scheme_name: Optional[str] = None,
        scopes: Optional[dict[str, str]] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ):
        if not scopes:
            scopes = {}
        flows = _OAuthFlowsModel(
            clientCredentials=cast(
                Any,
                {
                    "tokenUrl": tokenUrl,
                    "scopes": scopes,
                },
            )
        )
        super().__init__(
            flows=flows,
            scheme_name=scheme_name,
            description=description,
            auto_error=auto_error,
        )

    async def __call__(self, request: Request) -> Optional[str]:
        authorization = request.headers.get("Authorization")
        scheme, param = _get_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        return param
