"""Supporting data structures for Request and other Starlette-compatible types."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


class URL:
    """Starlette-compatible URL wrapper."""

    def __init__(self, scope_or_url=None):
        if isinstance(scope_or_url, str):
            self._url = scope_or_url
        elif isinstance(scope_or_url, dict):
            scheme = scope_or_url.get("scheme", "http")
            server = scope_or_url.get("server")
            if server:
                host, port = server
                if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                    netloc = host
                else:
                    netloc = f"{host}:{port}"
            else:
                netloc = "localhost"
            path = scope_or_url.get("path", "/")
            qs = scope_or_url.get("query_string", "")
            if isinstance(qs, bytes):
                qs = qs.decode("latin-1")
            self._url = f"{scheme}://{netloc}{path}"
            if qs:
                self._url += f"?{qs}"
        else:
            self._url = ""

        self._parsed = urlparse(self._url)

    @property
    def scheme(self) -> str:
        return self._parsed.scheme

    @property
    def hostname(self) -> str | None:
        return self._parsed.hostname

    @property
    def port(self) -> int | None:
        return self._parsed.port

    @property
    def netloc(self) -> str:
        return self._parsed.netloc

    @property
    def path(self) -> str:
        return self._parsed.path

    @property
    def query(self) -> str:
        return self._parsed.query

    @property
    def fragment(self) -> str:
        return self._parsed.fragment

    @property
    def components(self):
        return self._parsed

    def __str__(self) -> str:
        return self._url

    def __repr__(self) -> str:
        return f"URL({self._url!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, URL):
            return self._url == other._url
        if isinstance(other, str):
            return self._url == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._url)


class Headers:
    """Case-insensitive dict-like for HTTP headers."""

    def __init__(self, raw=None):
        self._dict: dict[str, str] = {}
        if raw is None:
            pass
        elif isinstance(raw, dict):
            for k, v in raw.items():
                self._dict[k.lower()] = str(v)
        elif isinstance(raw, (list, tuple)):
            # list of (key, value) tuples — common in ASGI scope
            for k, v in raw:
                if isinstance(k, bytes):
                    k = k.decode("latin-1")
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                self._dict[k.lower()] = v
        else:
            pass

    def __getitem__(self, key: str) -> str:
        return self._dict[key.lower()]

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key.lower() in self._dict
        return False

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._dict.get(key.lower(), default)

    def keys(self):
        return self._dict.keys()

    def values(self):
        return self._dict.values()

    def items(self):
        return self._dict.items()

    def __iter__(self):
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __repr__(self) -> str:
        return f"Headers({self._dict!r})"

    def getlist(self, key: str) -> list[str]:
        """Return all values for a header key (single value as one-element list)."""
        val = self._dict.get(key.lower())
        if val is None:
            return []
        return [val]


class QueryParams:
    """Dict-like for query parameters with multi-value support."""

    def __init__(self, raw=None):
        if raw is None:
            self._dict: dict[str, list[str]] = {}
        elif isinstance(raw, str):
            if isinstance(raw, bytes):
                raw = raw.decode("latin-1")
            self._dict = parse_qs(raw, keep_blank_values=True)
        elif isinstance(raw, bytes):
            self._dict = parse_qs(raw.decode("latin-1"), keep_blank_values=True)
        elif isinstance(raw, dict):
            self._dict = {}
            for k, v in raw.items():
                if isinstance(v, list):
                    self._dict[k] = v
                else:
                    self._dict[k] = [str(v)]
        else:
            self._dict = {}

    def __getitem__(self, key: str) -> str:
        return self._dict[key][0]

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key in self._dict
        return False

    def get(self, key: str, default: str | None = None) -> str | None:
        vals = self._dict.get(key)
        if vals:
            return vals[0]
        return default

    def getlist(self, key: str) -> list[str]:
        return self._dict.get(key, [])

    def keys(self):
        return self._dict.keys()

    def values(self):
        return (v[0] for v in self._dict.values())

    def items(self):
        return ((k, v[0]) for k, v in self._dict.items())

    def multi_items(self):
        for k, vs in self._dict.items():
            for v in vs:
                yield k, v

    def __iter__(self):
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __repr__(self) -> str:
        return f"QueryParams({self._dict!r})"

    def __bool__(self) -> bool:
        return bool(self._dict)


class Address:
    """Client address (host, port) pair."""

    def __init__(self, host_port_tuple):
        if isinstance(host_port_tuple, (list, tuple)) and len(host_port_tuple) >= 2:
            self.host = host_port_tuple[0]
            self.port = host_port_tuple[1]
        else:
            self.host = "0.0.0.0"
            self.port = 0

    def __repr__(self) -> str:
        return f"Address(host={self.host!r}, port={self.port!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Address):
            return self.host == other.host and self.port == other.port
        return NotImplemented


class State:
    """Simple attribute-based namespace for app/request state.

    Identical to ``types.SimpleNamespace`` but provided for Starlette
    compatibility so ``from starlette.datastructures import State`` works.
    """

    def __init__(self, state: dict[str, Any] | None = None, **kwargs: Any):
        if state:
            super().__setattr__("_state", dict(state))
        else:
            super().__setattr__("_state", {})
        self._state.update(kwargs)

    def __setattr__(self, name: str, value: Any) -> None:
        self._state[name] = value

    def __getattr__(self, name: str) -> Any:
        try:
            return self._state[name]
        except KeyError:
            message = f"'{type(self).__name__}' object has no attribute '{name}'"
            raise AttributeError(message) from None

    def __delattr__(self, name: str) -> None:
        try:
            del self._state[name]
        except KeyError:
            message = f"'{type(self).__name__}' object has no attribute '{name}'"
            raise AttributeError(message) from None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, State):
            return self._state == other._state
        return NotImplemented

    def __repr__(self) -> str:
        contents = ", ".join(f"{k}={v!r}" for k, v in self._state.items())
        return f"State({contents})"
