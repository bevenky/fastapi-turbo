"""Trusted Host middleware.

Validates the ``Host`` header against a list of allowed hostnames.
If the host is not in the allowed list, the request is rejected with
a 400 status code.

Note: Full ASGI-level interception requires Rust/Tower integration.
This implementation provides a Python-level ``validate_host`` helper
that can be called in middleware dispatch or used standalone.
"""

from __future__ import annotations

from typing import Any


class TrustedHostMiddleware:
    """FastAPI-compatible TrustedHost middleware.

    Validates the Host header against *allowed_hosts*.  A wildcard
    entry ``"*"`` permits all hosts.  Domain patterns like
    ``"*.example.com"`` match subdomains.
    """

    _fastapi_rs_middleware_type = "trustedhost"

    def __init__(self, app: Any = None, *, allowed_hosts: list[str] | None = None):
        self.app = app
        self.allowed_hosts = allowed_hosts or ["*"]
        self._allow_all = "*" in self.allowed_hosts

    def is_valid_host(self, host: str) -> bool:
        """Return True if *host* is permitted by the allowed_hosts list."""
        if self._allow_all:
            return True

        # Strip port from host if present
        if ":" in host:
            host = host.split(":")[0]

        host = host.lower()

        for pattern in self.allowed_hosts:
            pattern = pattern.lower()
            if pattern == host:
                return True
            # Wildcard subdomain matching: "*.example.com"
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True

        return False
