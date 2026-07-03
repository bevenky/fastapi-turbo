"""FastAPI drop-in signature pins NOT covered by the upstream suite.

CONSOLIDATION (coverage-differential, round-9 follow-up): this file kept only
the tests that either carry unique arcs over ``python/fastapi_turbo`` (per-test
coverage contexts vs the full retained suite + upstream FastAPI suite) or pin a
signature/behavior the upstream suite provably never exercises (grep evidence
against the 0.138.1 clone). Deleted-as-redundant, with their upstream twins:

  * set_cookie("k","v") two-positional      → tests/test_repeated_cookie_headers.py
  * FastAPI(exception_handlers={...})       → tests/test_exception_handlers.py
  * FastAPI(root_path=...) (+servers)       → tests/test_openapi_cache_root_path.py,
                                              behind_a_proxy tutorials
  * @app.exception_handler(cls) decorator   → tests/test_validation_error_context.py
  * @app.exception_handler(404) status form → local tests/test_new_features.py:455
  * @app.middleware("http") accept/reject   → local tests/test_new_features.py
                                              (TestHTTPMiddleware, exact same pins)
  * Body() embed default / embed=True       → tests/test_union_body_discriminator_*,
                                              multiple-body-params suite
  * Body(media_type=...)                    → tests/test_request_body_parameters_media_type.py
  * deprecated=None/True → OpenAPI          → upstream openapi deprecated tests
  * url_path_for value + URLPath-str        → tests/test_router_include_context.py
  * route-kwarg smoke (responses, callbacks,
    openapi_extra, security, tags, ...)     → exercised route-by-route upstream
  * the entire import-surface smoke         → every name grep-confirmed imported
                                              by upstream tests; module-walk lives
                                              in tests/test_shim_completeness.py
"""

from __future__ import annotations

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`


# ── Response.set_cookie: positional-or-keyword (Starlette-compatible) ──


def _set_cookies(resp):
    """Decoded Set-Cookie values from a (real Starlette) Response — its
    ``raw_headers`` are ``(bytes, bytes)`` and also carry content-length."""
    out = []
    for k, v in resp.raw_headers:
        kk = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        if kk.lower() == "set-cookie":
            out.append(v.decode("latin-1") if isinstance(v, bytes) else str(v))
    return out


class TestStarletteCookieSignature:
    """Positional call styles the upstream suite never uses — the
    parameter ORDER is the contract here."""

    def test_positional_all_args(self):
        """All positional args — matches Starlette."""
        from fastapi.responses import Response

        r = Response()
        r.set_cookie("k", "v", 3600, None, "/api", "example.com", True, True, "strict")
        value = _set_cookies(r)[0]
        assert "k=v" in value
        assert "Max-Age=3600" in value
        assert "Path=/api" in value
        assert "Domain=example.com" in value
        assert "Secure" in value
        assert "HttpOnly" in value
        assert "SameSite=strict" in value  # Starlette lowercases samesite

    def test_delete_cookie_positional(self):
        from fastapi.responses import Response

        r = Response()
        r.delete_cookie("k", "/", "example.com", True)
        value = _set_cookies(r)[0]
        assert "Path=/" in value
        assert "Domain=example.com" in value
        assert "Secure" in value
        assert "Max-Age=0" in value

    def test_partitioned_cookie(self):
        from fastapi.responses import Response

        r = Response()
        r.set_cookie("k", "v", partitioned=True)
        value = _set_cookies(r)[0]
        assert "Partitioned" in value


# ── Security() structural contract ────────────────────────────────


class TestSecurity:
    def test_security_is_depends_subclass(self):
        """User code doing ``isinstance(marker, Depends)`` must see
        Security as a Depends — upstream never asserts this shape."""
        from fastapi import Depends, Security

        s = Security(lambda: None, scopes=["me"])
        assert isinstance(s, Depends)
        assert s.scopes == ["me"]


# ── URLPath.make_absolute_url (unique-arc carrier) ─────────────────


class TestUrlPathFor:
    def test_make_absolute_url(self):
        """Starlette URLPath.make_absolute_url should work."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/users/{user_id}")
        def get_user(user_id: int):
            return {}

        url = app.url_path_for("get_user", user_id=1)
        abs_url = url.make_absolute_url("http://example.com")
        assert abs_url == "http://example.com/users/1"


# ── add_exception_handler imperative form (unique-arc carrier) ─────


class TestExceptionHandlerSignature:
    def test_add_exception_handler_imperative(self):
        """Starlette-style: app.add_exception_handler(...)"""
        from fastapi import FastAPI, HTTPException

        def h(req, exc):
            return {}

        app = FastAPI()
        app.add_exception_handler(HTTPException, h)
        assert app.exception_handlers[HTTPException] is h
