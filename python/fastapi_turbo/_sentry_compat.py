"""Sentry compat-shim helpers.

Stock Sentry integrates with FastAPI/Starlette by monkey-patching
``Starlette.__call__`` and ``fastapi.routing.get_request_handler``
at ``sentry_sdk.init(integrations=[FastApiIntegration()])`` time.
Our Rust HTTP server bypasses both, so those patches are inert here.
This module supplies the equivalent behaviour at the points where our
own Python dispatch runs:

* ``_ensure_sentry_middleware`` — on ``FastAPI.__init__``, auto-adds
  ``SentryAsgiMiddleware`` via ``add_middleware`` when a Sentry client
  with ``StarletteIntegration`` / ``FastApiIntegration`` is already
  active.
* ``_refine_sentry_transaction`` — inside the middleware shim, once
  the route has matched, relabels the transaction from URL-source to
  route/endpoint-source to match Sentry's own naming.
* ``_refine_sentry_transaction_as_middleware`` — when a middleware
  rejects a request without invoking the inner app (TrustedHost,
  Auth), records the middleware class as the transaction endpoint.
* ``_maybe_sentry_capture_failed_request`` — mirrors
  ``failed_request_status_codes`` for HTTPException raised from a
  handler (Starlette's ExceptionMiddleware is where Sentry hooks the
  status check; we do it directly).
* ``_maybe_install_sentry_request_event_processor`` — attaches an
  event processor that fills ``event.request.{data, cookies}`` using
  the request context Rust handed us.

All helpers are no-ops when Sentry isn't installed / active.

Keeping them in a dedicated module decouples this layer of compat
from the 7k-line ``applications.py`` and makes it easier to extend
(e.g. additional integrations) without touching core dispatch.
"""
from __future__ import annotations

import contextvars
from typing import Any


# ── Per-request scope ContextVar ───────────────────────────────────────
#
# Tracks the ASGI scope of the currently-executing request so that
# ``@app.exception_handler`` callbacks can receive a properly populated
# ``Request`` (real path, method, headers) instead of a bare stub.
# Set at the top of each request's Python handler pipeline; read by
# ``_invoke_exception_handler``. Request-scoped — resets on every
# request via the Rust bridge's per-request call.
_current_request_scope: contextvars.ContextVar = contextvars.ContextVar(
    "fastapi_turbo_current_request_scope", default=None
)


class _RouteScope:
    """Lightweight route-like object with a ``.path`` attribute.

    Sentry's ``StarletteIntegration`` reads ``request.scope["route"].path``
    for URL-style transaction naming. Our compiled route state doesn't
    match Starlette's Route class, so we wrap the path in this stub
    when populating the request scope.
    """

    __slots__ = ("path", "name", "endpoint")

    def __init__(self, path: str, name: str | None = None, endpoint=None):
        self.path = path
        self.name = name
        self.endpoint = endpoint


def set_current_request_scope(
    method: str | None,
    path: str | None,
    query_string: str | None,
    endpoint=None,
    route_path: str | None = None,
) -> None:
    """Populate the per-request ContextVar so exception handlers can see
    the real request path/method via ``request.url.path``.

    Called from the Rust bridge once per request (before the user
    handler dispatches). ``_refine_sentry_transaction`` is intentionally
    NOT called here — the handler may still be rejected by outer
    middleware, in which case the transaction should keep its default
    URL-source name.
    """
    scope: dict[str, Any] = {"type": "http"}
    if method:
        scope["method"] = method
    if path is not None:
        scope["path"] = path
        scope["raw_path"] = path.encode("latin-1")
    if query_string is not None:
        scope["query_string"] = query_string.encode("latin-1")
    if endpoint is not None:
        scope["endpoint"] = endpoint
    if route_path is not None:
        scope["route"] = _RouteScope(route_path, endpoint=endpoint)
    _current_request_scope.set(scope)


def refine_request_scope_for_route(endpoint, route_path: str | None) -> None:
    """Stamp ``endpoint`` and ``route`` onto the current request scope
    AND refresh Sentry's transaction name if a Sentry client is active.

    Called from inside compiled handler wrappers — at that point we
    know which route matched, so Sentry can switch from URL-source
    transactions to route-/endpoint-source ones.
    """
    scope = _current_request_scope.get()
    if scope is None:
        scope = {"type": "http"}
    scope = dict(scope)
    if endpoint is not None:
        scope["endpoint"] = endpoint
    if route_path is not None:
        scope["route"] = _RouteScope(route_path, endpoint=endpoint)
    _current_request_scope.set(scope)
    refine_sentry_transaction(endpoint, route_path)


# ── Sentry helpers ────────────────────────────────────────────────────


def _get_integration():
    """Return (client, integration) when Sentry is active and either
    FastAPI or Starlette integration is enabled, else (None, None)."""
    try:
        import sentry_sdk  # noqa: PLC0415
    except ImportError:
        return None, None
    try:
        client = sentry_sdk.get_client()
        if not (client and getattr(client, "is_active", lambda: False)()):
            return None, None
    except Exception:  # noqa: BLE001
        return None, None
    integration = None
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: PLC0415
        integration = client.get_integration(FastApiIntegration)
    except Exception:  # noqa: BLE001
        pass
    if integration is None:
        try:
            from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415
            integration = client.get_integration(StarletteIntegration)
        except Exception:  # noqa: BLE001
            return None, None
    if integration is None:
        return None, None
    return client, integration


def refine_sentry_transaction_as_middleware(mw_cls, mw_name: str) -> None:
    """Record a rejecting middleware as the Sentry transaction endpoint.

    Mirrors Sentry's ``patch_middlewares`` behaviour: when a middleware
    handles a request itself (doesn't call the inner app), Sentry
    treats that middleware class as the endpoint. For
    ``transaction_style="endpoint"`` the transaction name becomes the
    middleware's fully-qualified name with source ``component``; for
    ``transaction_style="url"`` the name stays URL-based and we don't
    touch it.
    """
    _client, integration = _get_integration()
    if integration is None:
        return
    style = getattr(integration, "transaction_style", "url")
    if style != "endpoint":
        return
    import sentry_sdk  # noqa: PLC0415
    try:
        from sentry_sdk.tracing import SOURCE_FOR_STYLE  # noqa: PLC0415
        source = SOURCE_FOR_STYLE.get("endpoint")
    except Exception:  # noqa: BLE001
        source = None
    try:
        scope = sentry_sdk.get_current_scope()
        if source is not None:
            scope.set_transaction_name(mw_name, source=source)
        else:
            scope.set_transaction_name(mw_name)
    except Exception:  # noqa: BLE001
        pass


def maybe_install_sentry_request_event_processor(kwargs) -> None:
    """Attach a Sentry scope event processor that enriches captured
    events with the current request's body + cookies (scrubbed by
    Sentry's default data-scrubber). Mirrors stock FastAPI's
    ``patch_get_request_handler`` extractor flow, which doesn't fire
    for us because the Rust router doesn't call
    ``fastapi.routing.get_request_handler``.
    """
    _client, integration = _get_integration()
    if integration is None:
        return
    import sentry_sdk  # noqa: PLC0415

    raw_body = kwargs.get("__fastapi_turbo_raw_body_bytes__")
    if raw_body is None:
        raw_body_str = kwargs.get("__fastapi_turbo_raw_body_str__")
        if isinstance(raw_body_str, (bytes, bytearray)):
            raw_body = bytes(raw_body_str)
        elif isinstance(raw_body_str, str):
            raw_body = raw_body_str.encode("utf-8")
    hdr_list = kwargs.get("_request_headers") or []
    cookies: dict = {}
    for k, v in hdr_list or []:
        try:
            kstr = k.decode("latin-1") if isinstance(k, bytes) else k
            vstr = v.decode("latin-1") if isinstance(v, bytes) else v
        except UnicodeDecodeError:
            continue
        if kstr.lower() == "cookie":
            from http.cookies import SimpleCookie  # noqa: PLC0415
            jar = SimpleCookie()
            try:
                jar.load(vstr)
                for ck, morsel in jar.items():
                    cookies[ck] = morsel.value
            except Exception:  # noqa: BLE001
                pass

    def _processor(event, hint):
        request_info = event.get("request") or {}
        if raw_body:
            try:
                import json as _json  # noqa: PLC0415
                parsed = _json.loads(raw_body.decode("utf-8"))
                request_info["data"] = parsed
            except Exception:  # noqa: BLE001
                try:
                    request_info["data"] = raw_body.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
        if cookies:
            request_info["cookies"] = cookies
        event["request"] = request_info
        return event

    try:
        sentry_sdk.get_isolation_scope().add_event_processor(_processor)
    except Exception:  # noqa: BLE001
        pass


def maybe_sentry_capture_failed_request(exc) -> None:
    """Mirror Sentry's StarletteIntegration
    ``failed_request_status_codes`` behaviour for HTTPException raised
    from a handler. Stock Starlette routes the exception through
    ExceptionMiddleware where Sentry's patch emits an event when the
    status is in the configured set; our dispatch converts HTTPException
    to a JSON response directly, so we emit the event ourselves.
    """
    if exc is None:
        return
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return
    try:
        import sentry_sdk  # noqa: PLC0415
    except ImportError:
        return
    try:
        client = sentry_sdk.get_client()
        if not (client and getattr(client, "is_active", lambda: False)()):
            return
        from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415
        integration = client.get_integration(StarletteIntegration)
        if integration is None:
            return
        codes = getattr(integration, "failed_request_status_codes", None)
        if codes is None or status not in codes:
            return
        try:
            sentry_sdk.capture_exception(exc)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return


def refine_sentry_transaction(endpoint, route_path: str | None) -> None:
    """Ask Sentry to re-label the current transaction from URL source
    to route/endpoint source. No-op when Sentry isn't active."""
    _client, integration = _get_integration()
    if integration is None:
        return
    style = getattr(integration, "transaction_style", "url")
    name = ""
    if style == "endpoint" and endpoint is not None:
        try:
            from sentry_sdk.utils import transaction_from_function  # noqa: PLC0415
            name = transaction_from_function(endpoint) or ""
        except Exception:  # noqa: BLE001
            name = getattr(endpoint, "__name__", "") or ""
    elif style == "url" and route_path is not None:
        name = route_path
    if not name:
        return
    import sentry_sdk  # noqa: PLC0415
    try:
        from sentry_sdk.tracing import SOURCE_FOR_STYLE  # noqa: PLC0415
        source = SOURCE_FOR_STYLE.get(style)
    except Exception:  # noqa: BLE001
        source = None
    try:
        sentry_scope = sentry_sdk.get_current_scope()
        if source is not None:
            sentry_scope.set_transaction_name(name, source=source)
        else:
            sentry_scope.set_transaction_name(name)
    except Exception:  # noqa: BLE001
        pass


def ensure_sentry_middleware(app) -> None:
    """Auto-install ``SentryAsgiMiddleware`` when a Sentry client with
    ``StarletteIntegration`` / ``FastApiIntegration`` is active.

    Stock Starlette/FastAPI gets Sentry tracing via a monkey-patch of
    ``Starlette.__call__``; our Rust HTTP server bypasses that entry,
    so the patch would be inert. Doing an explicit ``add_middleware``
    here routes the request through Sentry's ASGI wrapper via our
    ASGI middleware chain instead — transactions and error events
    flow end-to-end.
    """
    if getattr(app, "_fastapi_turbo_sentry_installed", False):
        return
    _client, integration = _get_integration()
    if integration is None:
        return
    try:
        from sentry_sdk.integrations.asgi import SentryAsgiMiddleware  # noqa: PLC0415
    except ImportError:
        return
    for _cls, _ in getattr(app, "_middleware_stack", []):
        if _cls is SentryAsgiMiddleware:
            app._fastapi_turbo_sentry_installed = True
            return
    for _cls, _ in getattr(app, "_raw_asgi_middlewares", []):
        if _cls is SentryAsgiMiddleware:
            app._fastapi_turbo_sentry_installed = True
            return

    # Seed the auto-installed middleware with the integration's
    # configuration so test-visible knobs propagate.
    mw_kwargs: dict = {}
    style = getattr(integration, "transaction_style", None)
    if style is not None:
        mw_kwargs["transaction_style"] = style
    hmtc = getattr(integration, "http_methods_to_capture", None)
    if hmtc is not None:
        mw_kwargs["http_methods_to_capture"] = hmtc
    origin = getattr(integration, "origin", None)
    if origin is not None:
        mw_kwargs["span_origin"] = origin
    mtype = getattr(integration, "identifier", None)
    if mtype is not None:
        mw_kwargs["mechanism_type"] = mtype
    try:
        app.add_middleware(SentryAsgiMiddleware, **mw_kwargs)
        app._fastapi_turbo_sentry_installed = True
    except Exception:  # noqa: BLE001
        try:
            app.add_middleware(SentryAsgiMiddleware)
            app._fastapi_turbo_sentry_installed = True
        except Exception:  # noqa: BLE001
            app._fastapi_turbo_sentry_installed = True


# Backwards-compat aliases: older applications.py call sites used
# underscore-prefixed names. Expose both so we can migrate gradually.
_refine_sentry_transaction = refine_sentry_transaction
_refine_sentry_transaction_as_middleware = refine_sentry_transaction_as_middleware
_maybe_sentry_capture_failed_request = maybe_sentry_capture_failed_request
_maybe_install_sentry_request_event_processor = (
    maybe_install_sentry_request_event_processor
)
_refine_request_scope_for_route = refine_request_scope_for_route
_set_current_request_scope = set_current_request_scope
_ensure_sentry_middleware = ensure_sentry_middleware
