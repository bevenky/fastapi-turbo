"""SessionMiddleware — signed cookie-based sessions (Starlette-compatible).

Populates ``request.session`` as a mutable dict. On response send, serializes
the session back into a signed cookie using itsdangerous (or stdlib fallback).

Usage::

    from fastapi_turbo.middleware.sessions import SessionMiddleware
    app.add_middleware(SessionMiddleware, secret_key="...", session_cookie="session")

    @app.get("/login")
    async def login(request: Request):
        request.session["user_id"] = 42
        return {"ok": True}

    @app.get("/me")
    async def me(request: Request):
        return {"user_id": request.session.get("user_id")}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from http.cookies import SimpleCookie
from typing import Any


DEFAULT_MAX_AGE = 14 * 24 * 3600  # 14 days


class SessionMiddleware:
    """HTTP middleware attaching a signed cookie-based session to each request.

    Compatible with ``@app.middleware('http')`` pattern. Reads the session
    cookie on the way in, decodes + verifies the signature, populates
    ``request.session``. On the way out, serializes the (possibly mutated)
    session dict back into the response as a Set-Cookie header.
    """

    _fastapi_turbo_middleware_type = "python_http_session"

    def __init__(
        self,
        app=None,
        *,
        secret_key: str,
        session_cookie: str = "session",
        max_age: int | None = DEFAULT_MAX_AGE,
        path: str = "/",
        same_site: str = "lax",
        https_only: bool = False,
        domain: str | None = None,
    ):
        self.app = app
        self.secret_key = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.same_site = same_site
        self.https_only = https_only
        self.domain = domain

    async def __call__(self, request, call_next):
        # Load: pull session from cookie
        cookie_header = _get_cookie_header(request)
        initial: dict[str, Any] = {}
        if cookie_header:
            sc = SimpleCookie()
            sc.load(cookie_header)
            morsel = sc.get(self.session_cookie)
            if morsel:
                initial = self._decode(morsel.value) or {}
        request.scope["session"] = initial

        response = await call_next(request)

        # Save: serialize session back into Set-Cookie
        session = request.scope.get("session", {})
        if session or initial:
            # Only emit Set-Cookie if the session is non-empty or was previously set
            cookie_value = self._encode(session)
            self._attach_cookie(response, cookie_value, clear=not session)
        return response

    # ── Sign + serialize ─────────────────────────────────────────────

    def _encode(self, data: dict) -> str:
        """Serialize + sign session dict into a URL-safe cookie value.

        Format: base64(json).timestamp.hmac_sha256 (matches itsdangerous-style).
        """
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        b64 = base64.urlsafe_b64encode(raw).rstrip(b"=")
        ts = str(int(time.time())).encode("utf-8")
        payload = b64 + b"." + ts
        sig = hmac.new(self.secret_key, payload, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
        return (payload + b"." + sig_b64).decode("ascii")

    def _decode(self, cookie_val: str) -> dict | None:
        """Verify signature + timestamp, return session dict or None on failure."""
        try:
            parts = cookie_val.encode("ascii").split(b".")
            if len(parts) != 3:
                return None
            b64, ts, sig_b64 = parts
            payload = b64 + b"." + ts
            expected = hmac.new(self.secret_key, payload, hashlib.sha256).digest()
            actual = base64.urlsafe_b64decode(sig_b64 + b"=" * (-len(sig_b64) % 4))
            if not hmac.compare_digest(expected, actual):
                return None
            # Check max_age
            if self.max_age is not None:
                age = int(time.time()) - int(ts)
                if age > self.max_age:
                    return None
            raw = base64.urlsafe_b64decode(b64 + b"=" * (-len(b64) % 4))
            return json.loads(raw)
        except Exception:
            return None

    # ── Response integration ─────────────────────────────────────────

    def _attach_cookie(self, response, value: str, clear: bool = False) -> None:
        """Append Set-Cookie to response.raw_headers (preserves duplicates)."""
        parts = [f"{self.session_cookie}={value if not clear else ''}"]
        if clear:
            parts.append("Max-Age=0")
        elif self.max_age is not None:
            parts.append(f"Max-Age={int(self.max_age)}")
        parts.append(f"Path={self.path}")
        if self.domain:
            parts.append(f"Domain={self.domain}")
        if self.https_only:
            parts.append("Secure")
        parts.append("HttpOnly")
        parts.append(f"SameSite={self.same_site.capitalize()}")
        cookie_str = "; ".join(parts)
        if hasattr(response, "raw_headers"):
            response.raw_headers.append(("set-cookie", cookie_str))
        elif hasattr(response, "headers"):
            # Fallback for plain dict returns wrapped mid-flight
            response.headers["set-cookie"] = cookie_str


def _get_cookie_header(request) -> str:
    """Extract the Cookie header from a request, handling multiple ASGI formats."""
    if hasattr(request, "headers"):
        headers = request.headers
        try:
            return headers.get("cookie", "") or ""
        except Exception:
            pass
    scope = getattr(request, "scope", {}) or {}
    for k, v in scope.get("headers", []):
        name = k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else k
        if name.lower() == "cookie":
            return v.decode("latin-1") if isinstance(v, (bytes, bytearray)) else v
    return ""
