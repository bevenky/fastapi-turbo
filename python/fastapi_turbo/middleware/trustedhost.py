"""Trusted Host middleware.

Validates the ``Host`` header against a list of allowed hostnames.
If the host is not in the allowed list, the request is rejected with
a 400 status code.

Implemented as a Python ASGI middleware (not the Rust/Tower fast path)
so it integrates with other ASGI middleware — SentryAsgiMiddleware
wraps around this one and sees the reject-with-400 as a normal response,
letting it emit a transaction span for the rejected request.
"""

from __future__ import annotations

from typing import Any


class TrustedHostMiddleware:
    """FastAPI / Starlette-compatible TrustedHost middleware.

    Validates the Host header against ``allowed_hosts``. A wildcard
    entry ``"*"`` permits all hosts; domain patterns like
    ``"*.example.com"`` match subdomains.
    """

    # Marker preserved for introspection (some parity tests check it),
    # but the dispatch goes through the Python ASGI chain rather than
    # the Rust/Tower fast path — running as a real ASGI middleware
    # lets ``SentryAsgiMiddleware`` wrap around it and record a
    # transaction event even when the host-check rejects the request.
    _fastapi_turbo_middleware_type = "trustedhost"

    # Masquerade as Starlette's class so ``transaction_from_function``
    # in Sentry (and any other tool that formats
    # ``f"{cls.__module__}.{cls.__qualname__}"``) produces the
    # canonical ``starlette.middleware.trustedhost.TrustedHostMiddleware``
    # name that third-party tests assert on.
    __module__ = "starlette.middleware.trustedhost"

    def __init__(
        self,
        app: Any = None,
        *,
        allowed_hosts: list[str] | None = None,
        www_redirect: bool = True,
    ):
        self.app = app
        self.allowed_hosts = list(allowed_hosts) if allowed_hosts else ["*"]
        self.www_redirect = www_redirect
        self._allow_all = "*" in self.allowed_hosts

    def is_valid_host(self, host: str) -> bool:
        """Return True if ``host`` is permitted by the allowed_hosts list."""
        if self._allow_all:
            return True
        if ":" in host:
            host = host.split(":")[0]
        host = host.lower()
        for pattern in self.allowed_hosts:
            pattern = pattern.lower()
            if pattern == host:
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
        return False

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            if self.app is not None:
                await self.app(scope, receive, send)
            return

        host_header: str = ""
        for name, value in scope.get("headers") or []:
            try:
                nstr = name.decode("latin-1") if isinstance(name, bytes) else name
            except UnicodeDecodeError:
                continue
            if nstr.lower() == "host":
                try:
                    host_header = (
                        value.decode("latin-1") if isinstance(value, bytes) else value
                    )
                except UnicodeDecodeError:
                    host_header = ""
                break

        if self.is_valid_host(host_header):
            if self.app is not None:
                await self.app(scope, receive, send)
            return

        # Reject. Starlette returns a 400 with a plain-text body.
        if scope["type"] == "http":
            body = b"Invalid host header"
            await send({
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            })
        else:
            # WebSocket: close with 1008 policy violation.
            await send({"type": "websocket.close", "code": 1008})
