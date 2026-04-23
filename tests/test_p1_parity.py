"""Tests for P1 FastAPI parity fixes.

Covers:
- OAuth2ClientCredentials / OAuth2AuthorizationCodeBearer / OpenIdConnect
- request.stream() chunk iterator
- request.auth / request.user properties
- SessionMiddleware
- AuthenticationMiddleware + @requires decorator
- Pydantic v2 decorators (computed_field, field_serializer, model_validator)
"""

from __future__ import annotations

import asyncio

import pytest


# ── OAuth2 / OIDC schemes ────────────────────────────────────────────


class TestSecuritySchemes:
    def test_oauth2_client_credentials(self):
        from fastapi_turbo.security import OAuth2ClientCredentials

        scheme = OAuth2ClientCredentials(tokenUrl="/token", scopes={"read": "r"})
        assert scheme.model["type"] == "oauth2"
        assert "clientCredentials" in scheme.model["flows"]
        assert scheme.model["flows"]["clientCredentials"]["tokenUrl"] == "/token"

    def test_oauth2_authorization_code(self):
        from fastapi_turbo.security import OAuth2AuthorizationCodeBearer

        scheme = OAuth2AuthorizationCodeBearer(
            authorizationUrl="/auth",
            tokenUrl="/token",
            refreshUrl="/refresh",
            scopes={"admin": "Admin"},
        )
        flow = scheme.model["flows"]["authorizationCode"]
        assert flow["authorizationUrl"] == "/auth"
        assert flow["tokenUrl"] == "/token"
        assert flow["refreshUrl"] == "/refresh"

    def test_openid_connect(self):
        from fastapi_turbo.security import OpenIdConnect

        scheme = OpenIdConnect(openIdConnectUrl="https://example.com/.well-known/openid-configuration")
        assert scheme.model["type"] == "openIdConnect"
        assert "openid-configuration" in scheme.model["openIdConnectUrl"]

    def test_imports_from_fastapi_turbo(self):
        from fastapi_turbo import (
            OAuth2AuthorizationCodeBearer,
            OAuth2ClientCredentials,
            OpenIdConnect,
        )

        assert OAuth2ClientCredentials is not None
        assert OAuth2AuthorizationCodeBearer is not None
        assert OpenIdConnect is not None

    def test_imports_via_starlette_shim(self):
        import fastapi_turbo  # noqa: F401

        from fastapi.security import (
            OAuth2AuthorizationCodeBearer,
            OAuth2ClientCredentials,
            OpenIdConnect,
        )

        assert OAuth2ClientCredentials is not None


# ── request.stream() ─────────────────────────────────────────────────


class TestRequestStream:
    def test_stream_yields_buffered_body(self):
        from fastapi_turbo.requests import Request

        req = Request(scope={"type": "http", "_body": b"hello world"})

        async def _consume():
            chunks = []
            async for c in req.stream():
                chunks.append(c)
            return chunks

        chunks = asyncio.run(_consume())
        assert chunks == [b"hello world", b""]

    def test_stream_empty_body(self):
        from fastapi_turbo.requests import Request

        req = Request(scope={"type": "http"})

        async def _consume():
            chunks = []
            async for c in req.stream():
                chunks.append(c)
            return chunks

        assert asyncio.run(_consume()) == [b""]

    def test_stream_yields_receive_chunks(self):
        from fastapi_turbo.requests import Request

        # Mock ASGI receive callable that yields 3 chunks
        chunks_to_yield = [
            {"body": b"part-1-", "more_body": True},
            {"body": b"part-2-", "more_body": True},
            {"body": b"part-3", "more_body": False},
        ]
        idx = [0]

        async def receive():
            msg = chunks_to_yield[idx[0]]
            idx[0] += 1
            return msg

        req = Request(scope={"type": "http"}, receive=receive)

        async def _consume():
            chunks = []
            async for c in req.stream():
                chunks.append(c)
            return chunks

        chunks = asyncio.run(_consume())
        # First 3 chunks are data, last is the empty sentinel
        assert chunks == [b"part-1-", b"part-2-", b"part-3", b""]


# ── request.auth / request.user ──────────────────────────────────────


class TestRequestAuthUser:
    def test_unauthenticated_by_default(self):
        from fastapi_turbo.authentication import UnauthenticatedUser
        from fastapi_turbo.requests import Request

        req = Request(scope={"type": "http"})
        assert isinstance(req.user, UnauthenticatedUser)
        assert not req.user.is_authenticated
        assert bool(req.user) is False

    def test_auth_scopes_empty_by_default(self):
        from fastapi_turbo.authentication import AuthCredentials
        from fastapi_turbo.requests import Request

        req = Request(scope={"type": "http"})
        assert isinstance(req.auth, AuthCredentials)
        assert req.auth.scopes == []

    def test_authenticated_user_reachable(self):
        from fastapi_turbo.authentication import AuthCredentials, SimpleUser
        from fastapi_turbo.requests import Request

        user = SimpleUser("alice")
        creds = AuthCredentials(["authenticated", "admin"])
        req = Request(scope={"type": "http", "user": user, "auth": creds})
        assert req.user is user
        assert req.user.is_authenticated
        assert req.user.display_name == "alice"
        assert "admin" in req.auth.scopes


# ── SessionMiddleware ────────────────────────────────────────────────


class TestSessionMiddleware:
    def test_sign_and_decode_roundtrip(self):
        from fastapi_turbo.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        encoded = mw._encode({"user_id": 42, "name": "Alice"})
        decoded = mw._decode(encoded)
        assert decoded == {"user_id": 42, "name": "Alice"}

    def test_bad_signature_rejected(self):
        from fastapi_turbo.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        other_mw = SessionMiddleware(secret_key="different-secret")
        encoded = mw._encode({"x": 1})
        assert other_mw._decode(encoded) is None

    def test_tampered_cookie_rejected(self):
        from fastapi_turbo.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        encoded = mw._encode({"x": 1})
        # Flip a bit in the signature
        tampered = encoded[:-4] + "AAAA"
        assert mw._decode(tampered) is None

    def test_app_middleware_registration(self):
        from fastapi_turbo import FastAPI
        from fastapi_turbo.middleware.sessions import SessionMiddleware

        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="s3cr3t")
        # SessionMiddleware should go to the Python HTTP middleware chain
        assert len(app._http_middlewares) == 1
        # Not the Tower stack
        assert not any(
            getattr(c, "_fastapi_turbo_middleware_type", "") == "python_http_session"
            for c, _ in app._middleware_stack
        )


# ── AuthenticationMiddleware ─────────────────────────────────────────


class TestAuthenticationMiddleware:
    def test_backend_populates_auth_and_user(self):
        """AuthenticationBackend.authenticate() result ends up on request.scope."""
        from fastapi_turbo.authentication import (
            AuthCredentials,
            AuthenticationBackend,
            AuthenticationMiddleware,
            SimpleUser,
        )
        from fastapi_turbo.requests import Request

        class TokenBackend(AuthenticationBackend):
            async def authenticate(self, request):
                token = request.scope.get("_test_token")
                if not token:
                    return None
                return AuthCredentials(["authenticated", "user"]), SimpleUser("bob")

        mw = AuthenticationMiddleware(backend=TokenBackend())
        req = Request(scope={"type": "http", "_test_token": "xyz"})

        async def call_next(r):
            return {"user": r.user.username, "scopes": list(r.auth.scopes)}

        result = asyncio.run(mw(req, call_next))
        assert result["user"] == "bob"
        assert "user" in result["scopes"]

    def test_no_token_stays_unauthenticated(self):
        from fastapi_turbo.authentication import (
            AuthenticationBackend,
            AuthenticationMiddleware,
        )
        from fastapi_turbo.requests import Request

        class Backend(AuthenticationBackend):
            async def authenticate(self, request):
                return None

        mw = AuthenticationMiddleware(backend=Backend())
        req = Request(scope={"type": "http"})

        async def call_next(r):
            return {"authed": r.user.is_authenticated}

        assert asyncio.run(mw(req, call_next)) == {"authed": False}


class TestRequiresDecorator:
    def test_missing_scope_returns_403(self):
        from fastapi_turbo.authentication import (
            AuthCredentials,
            SimpleUser,
            requires,
        )
        from fastapi_turbo.requests import Request

        @requires("admin")
        async def secret(request: Request):
            return {"ok": True}

        req = Request(scope={
            "type": "http",
            "auth": AuthCredentials(["authenticated"]),
            "user": SimpleUser("bob"),
        })
        resp = asyncio.run(secret(request=req))
        assert resp.status_code == 403

    def test_has_scope_runs_handler(self):
        from fastapi_turbo.authentication import (
            AuthCredentials,
            SimpleUser,
            requires,
        )
        from fastapi_turbo.requests import Request

        @requires(["authenticated", "admin"])
        async def admin_page(request: Request):
            return {"ok": True}

        req = Request(scope={
            "type": "http",
            "auth": AuthCredentials(["authenticated", "admin"]),
            "user": SimpleUser("bob"),
        })
        assert asyncio.run(admin_page(request=req)) == {"ok": True}


# ── Pydantic v2 decorators ───────────────────────────────────────────


class TestPydanticV2Decorators:
    def test_computed_field_in_response(self):
        from pydantic import BaseModel, computed_field

        from fastapi_turbo import FastAPI

        class User(BaseModel):
            first_name: str
            last_name: str

            @computed_field
            @property
            def full_name(self) -> str:
                return f"{self.first_name} {self.last_name}"

        app = FastAPI()

        @app.get("/u", response_model=User)
        def get_user():
            return {"first_name": "Alice", "last_name": "Smith"}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert result.get("full_name") == "Alice Smith"

    def test_field_serializer(self):
        from pydantic import BaseModel, field_serializer

        from fastapi_turbo import FastAPI

        class M(BaseModel):
            tags: list[str]

            @field_serializer("tags")
            def _serialize_tags(self, value: list[str]) -> str:
                return ",".join(value)

        app = FastAPI()

        @app.get("/m", response_model=M)
        def get_m():
            return {"tags": ["a", "b", "c"]}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert result == {"tags": "a,b,c"}

    def test_model_validator(self):
        from pydantic import BaseModel, model_validator

        from fastapi_turbo import FastAPI

        class Config(BaseModel):
            enabled: bool
            timeout: int = 30

            @model_validator(mode="after")
            def _check(self):
                if self.enabled and self.timeout <= 0:
                    raise ValueError("timeout must be positive when enabled")
                return self

        app = FastAPI()

        @app.get("/c", response_model=Config)
        def get_config():
            return {"enabled": True, "timeout": 60}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert result == {"enabled": True, "timeout": 60}
