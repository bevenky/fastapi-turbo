"""Supporting data structures for Request and other Starlette-compatible types."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse


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
    """Dict-like for query parameters with multi-value support.

    Preserves declaration order of the raw query string — Starlette's
    `multi_items()` yields pairs in the exact order they appeared. A
    `parse_qs`-based implementation would group by key and lose that
    ordering; we use `parse_qsl` to keep insertion order.
    """

    def __init__(self, raw=None):
        self._items: list[tuple[str, str]] = []
        if raw is None:
            pass
        elif isinstance(raw, str):
            self._items = list(parse_qsl(raw, keep_blank_values=True))
        elif isinstance(raw, bytes):
            self._items = list(parse_qsl(raw.decode("latin-1"), keep_blank_values=True))
        elif isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, list):
                    for item in v:
                        self._items.append((k, str(item)))
                else:
                    self._items.append((k, str(v)))
        elif isinstance(raw, (list, tuple)):
            for pair in raw:
                if len(pair) == 2:
                    self._items.append((str(pair[0]), str(pair[1])))

    def __getitem__(self, key: str) -> str:
        # Starlette/FastAPI: when a key appears multiple times the LAST
        # occurrence wins for dict-style access; `getlist()` still returns
        # the full ordered list.
        val = None
        found = False
        for k, v in self._items:
            if k == key:
                val = v
                found = True
        if not found:
            raise KeyError(key)
        return val

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return any(k == key for k, _ in self._items)
        return False

    def get(self, key: str, default: str | None = None) -> str | None:
        val = default
        for k, v in self._items:
            if k == key:
                val = v
        return val

    def getlist(self, key: str) -> list[str]:
        return [v for k, v in self._items if k == key]

    def keys(self):
        seen: set[str] = set()
        for k, _ in self._items:
            if k not in seen:
                seen.add(k)
                yield k

    def values(self):
        # Starlette: yield only the last value per unique key.
        last: dict[str, str] = {}
        for k, v in self._items:
            last[k] = v
        return iter(last.values())

    def items(self):
        last: dict[str, str] = {}
        for k, v in self._items:
            last[k] = v
        return iter(last.items())

    def multi_items(self):
        # Yield in original order (insertion order of the raw query string).
        return iter(self._items)

    def __iter__(self):
        return self.keys()

    def __len__(self) -> int:
        seen: set[str] = set()
        for k, _ in self._items:
            seen.add(k)
        return len(seen)

    def __repr__(self) -> str:
        return f"QueryParams({self._items!r})"

    def __bool__(self) -> bool:
        return bool(self._items)


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


class URLPath(str):
    """Starlette-compatible URLPath — a str subclass with protocol + host.

    Returned by ``request.url_for(name, **params)`` / ``app.url_path_for(...)``.
    Call ``.make_absolute_url(base_url)`` to materialise a full URL string.
    """

    def __new__(cls, path: str = "", protocol: str = "", host: str = ""):
        instance = super().__new__(cls, path)
        instance._protocol = protocol
        instance._host = host
        return instance

    @property
    def protocol(self) -> str:
        return getattr(self, "_protocol", "")

    @property
    def host(self) -> str:
        return getattr(self, "_host", "")

    def make_absolute_url(self, base_url) -> str:
        base = str(base_url).rstrip("/")
        return base + str(self)


class MutableHeaders(Headers):
    """Headers that support __setitem__/__delitem__/append (Starlette-compat)."""

    def __setitem__(self, key: str, value: str) -> None:
        self._dict[key.lower()] = str(value)

    def __delitem__(self, key: str) -> None:
        del self._dict[key.lower()]

    def setdefault(self, key: str, value: str) -> str:
        return self._dict.setdefault(key.lower(), str(value))

    def update(self, other) -> None:
        if isinstance(other, Headers):
            for k, v in other.items():
                self._dict[k.lower()] = v
        elif isinstance(other, dict):
            for k, v in other.items():
                self._dict[k.lower()] = str(v)

    def append(self, key: str, value: str) -> None:
        """Append adds (or overwrites in this single-value impl). Starlette's
        append preserves duplicates — our simplified store collapses them."""
        self._dict[key.lower()] = str(value)


class FormData:
    """Multipart/urlencoded form-data wrapper — dict-like multi-value.

    Starlette-compat: ``form.get("name")``, ``form.getlist("name")``,
    ``form.items()``, iterate over keys. Can hold both str values and
    UploadFile instances.
    """

    def __init__(self, items=None):
        self._items: list[tuple[str, Any]] = []
        if items is None:
            return
        if isinstance(items, dict):
            for k, v in items.items():
                self._items.append((k, v))
        elif isinstance(items, (list, tuple)):
            for pair in items:
                if len(pair) == 2:
                    self._items.append((pair[0], pair[1]))
        elif isinstance(items, FormData):
            self._items = list(items._items)

    def __getitem__(self, key: str):
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return any(k == key for k, _ in self._items)

    def get(self, key: str, default=None):
        for k, v in self._items:
            if k == key:
                return v
        return default

    def getlist(self, key: str) -> list:
        return [v for k, v in self._items if k == key]

    def keys(self):
        seen = set()
        for k, _ in self._items:
            if k not in seen:
                seen.add(k)
                yield k

    def values(self):
        for _, v in self._items:
            yield v

    def items(self):
        return iter(self._items)

    def multi_items(self):
        return iter(self._items)

    def __iter__(self):
        return self.keys()

    def __len__(self) -> int:
        return len(self._items)


class Secret:
    """Wrapper that hides its value in repr(). For env-var secrets.

    Matches ``starlette.datastructures.Secret``.
    """

    def __init__(self, value: str):
        self._value = value

    def __repr__(self) -> str:
        return "Secret('**********')"

    def __str__(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)


class DefaultPlaceholder:
    """Internal sentinel used by FastAPI to detect "not explicitly set".

    Routers use this to distinguish between a user passing ``None`` and
    not passing a value at all, allowing router-level defaults to merge
    properly with route-level overrides.
    """

    def __init__(self, value: Any):
        self.value = value

    def __bool__(self) -> bool:
        return bool(self.value)

    def __repr__(self) -> str:
        return f"DefaultPlaceholder({self.value!r})"


def Default(value: Any) -> DefaultPlaceholder:
    """Create a :class:`DefaultPlaceholder` wrapping *value*."""
    return DefaultPlaceholder(value)


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
