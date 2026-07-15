"""P1 FastAPI parity pins NOT covered by the upstream suite.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * OAuth2 extension imports via fastapi.security
                                → retained test_oauth2_client_credentials
                                  below (imports the same path) + retained
                                  top-level import pin
  * computed_field in response_model
                                → tests/test_computed_fields.py
  * field_serializer in response_model
                                → tests/test_datetime_custom_encoder.py +
                                  tests/test_inherited_custom_class.py

KEPT: OAuth2ClientCredentials / OAuth2AuthorizationCodeBearer (turbo-only
extensions — grep over the 0.138.1 clone finds NO OAuth2ClientCredentials;
unique-line carriers in security.py), the request.stream() pins (door
``_door_make_request`` internals + chunked-receive shape with no upstream
``.stream()`` test), request.user/auth (no upstream twin), SessionMiddleware
and AuthenticationMiddleware/requires (turbo's own sessions.py /
authentication.py — unique-line carriers), and model_validator (no
upstream response-side model_validator test).

KEPT AS LOCAL-COVERAGE CARRIER (twin exists upstream —
tests/test_security_openid_connect.py — but the local fast suite would
lose the security.py OpenIdConnect arcs; ≤0.2% local-coverage gate):
test_openid_connect.
"""

from __future__ import annotations

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import asyncio

import pytest


# ── OAuth2 / OIDC schemes ────────────────────────────────────────────


class TestSecuritySchemes:
    # ``.model`` is the real pydantic SecurityScheme model (the clone's
    # dict-shaped ``.model`` is retired).

    def test_oauth2_client_credentials(self):
        from fastapi.security import OAuth2ClientCredentials

        scheme = OAuth2ClientCredentials(tokenUrl="/token", scopes={"read": "r"})
        assert scheme.model.type_.value == "oauth2"
        assert scheme.model.flows.clientCredentials is not None
        assert scheme.model.flows.clientCredentials.tokenUrl == "/token"

    def test_oauth2_authorization_code(self):
        from fastapi.security import OAuth2AuthorizationCodeBearer

        scheme = OAuth2AuthorizationCodeBearer(
            authorizationUrl="/auth",
            tokenUrl="/token",
            refreshUrl="/refresh",
            scopes={"admin": "Admin"},
        )
        flow = scheme.model.flows.authorizationCode
        assert flow.authorizationUrl == "/auth"
        assert flow.tokenUrl == "/token"
        assert flow.refreshUrl == "/refresh"

    def test_openid_connect(self):
        from fastapi.security import OpenIdConnect

        scheme = OpenIdConnect(openIdConnectUrl="https://example.com/.well-known/openid-configuration")
        assert scheme.model.type_.value == "openIdConnect"
        assert "openid-configuration" in scheme.model.openIdConnectUrl

    def test_imports_from_fastapi_turbo(self):
        from fastapi import (
            OAuth2AuthorizationCodeBearer,
            OAuth2ClientCredentials,
            OpenIdConnect,
        )

        assert OAuth2ClientCredentials is not None
        assert OAuth2AuthorizationCodeBearer is not None
        assert OpenIdConnect is not None


# ── request.stream() ─────────────────────────────────────────────────


class TestRequestStream:
    def test_stream_yields_buffered_body(self):
        # The door buffers the body into the _body attr (real Starlette
        # stream() short-circuits on it) via _door_make_request.
        from fastapi_turbo.requests import _door_make_request

        req = _door_make_request({"type": "http", "_body": b"hello world"})

        async def _consume():
            chunks = []
            async for c in req.stream():
                chunks.append(c)
            return chunks

        chunks = asyncio.run(_consume())
        assert chunks == [b"hello world", b""]

    def test_stream_empty_body(self):
        from fastapi_turbo.requests import _door_make_request

        req = _door_make_request({"type": "http"})

        async def _consume():
            chunks = []
            async for c in req.stream():
                chunks.append(c)
            return chunks

        # real Starlette stream() yields self._body then the b"" sentinel
        assert asyncio.run(_consume()) == [b"", b""]

    def test_stream_yields_receive_chunks(self):
        from starlette.requests import Request

        # Mock ASGI receive — real Starlette stream() reads message["type"].
        chunks_to_yield = [
            {"type": "http.request", "body": b"part-1-", "more_body": True},
            {"type": "http.request", "body": b"part-2-", "more_body": True},
            {"type": "http.request", "body": b"part-3", "more_body": False},
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
    def test_user_requires_authentication_middleware(self):
        # Parity with Starlette: accessing ``request.user`` without
        # AuthenticationMiddleware installed must raise — silently
        # returning UnauthenticatedUser hides misconfigured auth.
        from starlette.requests import Request

        req = Request(scope={"type": "http"})
        with pytest.raises(AssertionError):
            _ = req.user

    def test_auth_requires_authentication_middleware(self):
        from starlette.requests import Request

        req = Request(scope={"type": "http"})
        with pytest.raises(AssertionError):
            _ = req.auth

    def test_authenticated_user_reachable(self):
        from starlette.authentication import AuthCredentials, SimpleUser
        from starlette.requests import Request

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
        from starlette.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        encoded = mw._encode({"user_id": 42, "name": "Alice"})
        decoded = mw._decode(encoded)
        assert decoded == {"user_id": 42, "name": "Alice"}

    def test_bad_signature_rejected(self):
        from starlette.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        other_mw = SessionMiddleware(secret_key="different-secret")
        encoded = mw._encode({"x": 1})
        assert other_mw._decode(encoded) is None

    def test_tampered_cookie_rejected(self):
        from starlette.middleware.sessions import SessionMiddleware

        mw = SessionMiddleware(secret_key="test-secret")
        encoded = mw._encode({"x": 1})
        # Flip a bit in the signature
        tampered = encoded[:-4] + "AAAA"
        assert mw._decode(tampered) is None

    def test_app_middleware_registration(self):
        from fastapi import FastAPI
        from starlette.middleware.sessions import SessionMiddleware

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
        from starlette.authentication import (
            AuthCredentials,
            AuthenticationBackend,
            AuthenticationMiddleware,
            SimpleUser,
        )
        from starlette.requests import Request

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
        from starlette.authentication import (
            AuthenticationBackend,
            AuthenticationMiddleware,
        )
        from starlette.requests import Request

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
        from starlette.authentication import (
            AuthCredentials,
            SimpleUser,
            requires,
        )
        from starlette.requests import Request

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
        from starlette.authentication import (
            AuthCredentials,
            SimpleUser,
            requires,
        )
        from starlette.requests import Request

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
    def test_model_validator(self):
        from pydantic import BaseModel, model_validator

        from fastapi import FastAPI

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
