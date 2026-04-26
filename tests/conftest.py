"""Suite-level conftest: gracefully degrade when the test environment
denies loopback binds.

Some sandboxes (review/audit environments, restricted CI runners,
serverless dev shells) deny ``socket.bind(("127.0.0.1", 0))`` with
``PermissionError`` or ``OSError``. Tests that rely on launching
either the Rust loopback server (via ``TestClient``) or a subprocess
``Popen``-backed server thread crash en masse with that error.

We probe the environment ONCE at session start. If loopback binds
work, every test runs as before. If they don't, a session-level
``LOOPBACK_DENIED`` marker is set; tests opt out via
``@pytest.mark.requires_loopback``.

Test classification (informal):

  * **Real-server / Rust path tests** — exercise the Rust + Tower
    + socket pipeline. Use ``TestClient(app)`` (default), the
    ``server_app`` fixture (subprocess server), or directly bind
    a loopback port. Tagged ``@pytest.mark.requires_loopback`` so
    they skip cleanly in sandboxes that can't bind.

  * **ASGI-fallback / in-process parity tests** — exercise the
    Python ASGI dispatcher directly via ``TestClient(app,
    in_process=True)`` or ``httpx.AsyncClient(transport=
    httpx.ASGITransport(app=app))``. These don't bind anything
    and run unchanged in both modes.

When loopback IS denied, the ``server_app`` fixture below
transparently falls back to in-process exec: it exec's the test's
app source, registers it with a ``httpx`` redirect, and returns a
synthetic URL. The same test code passes in both modes — but the
fallback validates the ASGI / Python path, NOT the Rust / Tower /
socket path. Real-server invariants (``cli._port``, the Rust
HTTPSRedirect's ``X-Forwarded-Proto`` handling, real concurrent
sockets, subprocess WS protocol negotiation) need a real bind and
stay tagged ``requires_loopback``.

This split is what the audit calls for: "Tests that require real
ports should explicitly force/skip real-server mode, while
fallback tests should not assert ``_port`` or ``_app_servers``."
"""
from __future__ import annotations

import os
import socket

import pytest


def _can_bind_loopback() -> bool:
    """Probe whether ``socket.bind(('127.0.0.1', 0))`` succeeds.

    Returns ``True`` on a normal dev box, ``False`` in restricted
    sandboxes that raise ``PermissionError`` / ``OSError``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return True
    except (PermissionError, OSError):
        return False


def _loopback_denied() -> bool:
    """Determine whether to treat the env as loopback-denied.

    Auditors and CI runs that want to exercise the
    ``requires_loopback`` skip path can set
    ``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1`` to override the probe
    — useful when the actual ``socket.bind`` call from the test
    process succeeds (e.g. because the auditor's parent process is
    privileged) but a child process / subprocess server would fail.
    The env-var override flips ``LOOPBACK_DENIED`` to ``True``
    regardless of the probe result. The reverse override
    (``FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED=1``) lets a sandbox env
    that wants the full real-loopback suite to run override a
    failing probe, on the user's promise that bind will work for
    the actual subprocess servers."""
    if os.environ.get("FASTAPI_TURBO_FORCE_LOOPBACK_DENIED") == "1":
        return True
    if os.environ.get("FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED") == "1":
        return False
    return not _can_bind_loopback()


# Computed once per session — used both for module-level decisions
# and by per-test fixtures.
LOOPBACK_DENIED: bool = _loopback_denied()


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``requires_loopback`` marker so ``-m`` filters
    work and ``--strict-markers`` doesn't flag it as unknown."""
    config.addinivalue_line(
        "markers",
        "requires_loopback: skip when the environment denies "
        "``socket.bind(('127.0.0.1', 0))`` (sandboxed audit / "
        "restricted-CI environments).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-skip any test marked ``requires_loopback`` when loopback
    binds are denied AND the in-process httpx-redirect autouse
    fixture isn't available (sentinel for tests that genuinely need
    a real subprocess server, e.g. concurrent-clients stress)."""
    if not LOOPBACK_DENIED:
        return
    # Tests that actively use the in-process httpx redirect (every
    # ``server_app`` fixture below routes through it) DON'T need the
    # marker — they work fine via ASGI redirect. The marker is for
    # tests that genuinely need a real port: process-startup checks,
    # ``cli._port``-asserting lifecycle tests, multi-process WS, etc.
    skipper = pytest.mark.skip(
        reason="loopback bind denied by environment; this test needs "
        "a real port (subprocess server, lifecycle assertions, or "
        "concurrent-socket stress). Use TestClient(in_process=True) "
        "for behavioural tests, or run on an env that allows "
        "127.0.0.1:0 binds."
    )
    for item in items:
        if "requires_loopback" in item.keywords:
            item.add_marker(skipper)


# ────────────────────────────────────────────────────────────────────
# In-process ``httpx`` redirect for sandbox runs.
#
# Many test files use a per-file ``server_app`` fixture that spawns a
# subprocess server and returns its base URL. Tests then call
# ``httpx.get(f"{url}/path")`` directly. When loopback bind is
# denied, the subprocess can't start — and even if we could redirect
# through TestClient, the test code uses module-level ``httpx.get``
# which goes to the real network.
#
# This autouse fixture redirects ``httpx.get`` / ``post`` / ``put``
# / ``delete`` / ``patch`` / ``head`` / ``request`` through an
# ASGITransport when LOOPBACK_DENIED, IF the test has registered
# an active app via the helper below. Tests that don't register an
# app pass through unmodified (so non-server tests aren't affected).
# ────────────────────────────────────────────────────────────────────

# Per-test mapping ``base_url_prefix -> app`` for sandbox redirect.
_ACTIVE_INPROC_APPS: dict[str, object] = {}


def register_inproc_app(base_url: str, app) -> None:
    """Register an ASGI app to handle httpx requests for the given
    base URL. Used by sandbox-fallback ``server_app`` fixtures."""
    _ACTIVE_INPROC_APPS[base_url.rstrip("/")] = app


def unregister_inproc_app(base_url: str) -> None:
    _ACTIVE_INPROC_APPS.pop(base_url.rstrip("/"), None)


@pytest.fixture()
def server_app(tmp_path):
    """Sandbox-aware ``server_app`` factory.

    Tests pass an app source string with ``__PORT__`` placeholder and
    receive a base URL they can hit with ``httpx``. On a normal dev
    box (``LOOPBACK_DENIED == False``) we spawn a subprocess server
    on a free port, wait for the socket, and return its URL — exact
    parity with the previously-duplicated per-file fixtures. In a
    sandbox where loopback bind is denied, we exec the code
    in-process, extract ``app``, register it with the conftest's
    httpx redirect under a synthetic ``http://testserver-N`` host,
    and return that URL. Existing tests that do
    ``httpx.get(f"{url}/path")`` work unchanged in both modes.

    The exec'd code's ``app.run(...)`` is replaced with a no-op so
    the in-process exec doesn't try to bind a port; ``__PORT__``
    still gets substituted to keep string semantics identical."""
    import socket
    import subprocess
    import sys
    import textwrap
    import time
    import uuid

    procs: list = []
    in_proc_prefixes: list[str] = []

    def _start(code: str) -> str:
        if not LOOPBACK_DENIED:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            patched = code.replace("__PORT__", str(port))
            app_file = tmp_path / "app.py"
            app_file.write_text(textwrap.dedent(patched))
            proc = subprocess.Popen(
                [sys.executable, str(app_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=0.5
                    ):
                        break
                except (ConnectionRefusedError, OSError):
                    time.sleep(0.1)
                    if proc.poll() is not None:
                        out = proc.stdout.read().decode()
                        err = proc.stderr.read().decode()
                        pytest.fail(
                            f"Server died on startup.\nstdout: {out}\nstderr: {err}"
                        )
            else:
                proc.kill()
                pytest.fail("Server did not start in time")
            return f"http://127.0.0.1:{port}"

        # Sandbox path: exec the code in-process, replace
        # ``app.run(...)`` with a no-op, and route httpx through
        # ASGITransport.
        patched = textwrap.dedent(code).replace("__PORT__", "0")
        # Replace ``app.run(...)`` with a comment-only no-op so
        # exec'ing in-process doesn't try to bind. Preserves the
        # original line's indentation so the result still parses
        # (an unindented ``pass`` at the wrong nesting level
        # produces ``IndentationError: unexpected indent``).
        import re as _re
        patched = _re.sub(
            r"^(\s*)app\.run\([^)]*\)\s*$",
            r"\1pass  # app.run() suppressed in sandbox in-process mode",
            patched,
            flags=_re.M,
        )
        ns: dict = {}
        exec(compile(patched, str(tmp_path / "app.py"), "exec"), ns, ns)
        app = ns.get("app")
        if app is None:
            pytest.fail(
                "in-process exec produced no ``app`` global — "
                "test code must define ``app = FastAPI()``"
            )
        # Trigger pre-bind setup (lifespan startup + OpenAPI route)
        # so ``GET /openapi.json`` and lifespan-dependent endpoints
        # work via the in-process redirect — same setup ``app.run()``
        # would have done. Errors PROPAGATE: a startup hook that
        # raises is a real bug, and silently swallowing it would
        # turn broken state into passing assertions.
        installer = getattr(app, "_install_in_process_dynamic_routes", None)
        if callable(installer):
            installer()
        prefix = f"http://testserver-{uuid.uuid4().hex[:8]}"
        register_inproc_app(prefix, app)
        in_proc_prefixes.append(prefix)
        return prefix

    yield _start

    for p in procs:
        try:
            p.kill()
            p.wait()
        except Exception:  # noqa: BLE001
            pass
    for prefix in in_proc_prefixes:
        unregister_inproc_app(prefix)


@pytest.fixture(autouse=True)
def _httpx_inproc_redirect(monkeypatch):
    """Monkey-patch ``httpx.get`` / etc to route through the
    registered ASGI app when LOOPBACK_DENIED. No-op otherwise."""
    if not LOOPBACK_DENIED:
        yield
        return

    import httpx

    def _resolve_app(url: str):
        for prefix, app in _ACTIVE_INPROC_APPS.items():
            if url.startswith(prefix):
                return app, prefix
        return None, None

    def _patched_request(method: str, url: str, **kwargs):
        app, prefix = _resolve_app(url)
        if app is None:
            # No active app for this URL — fall through to real
            # httpx (which will fail with a real-network error,
            # but at least the test gets a clear signal).
            return _real_request(method, url, **kwargs)
        # Route through ``httpx.AsyncClient`` + ``ASGITransport``
        # under a fresh event loop. ``ASGITransport`` is async-only,
        # so we can't use it inside a sync ``httpx.Client``. Each
        # call gets its own ``asyncio.run`` — fine for tests since
        # they're already sync code that doesn't hold a running loop.
        rel = url[len(prefix):] or "/"
        import asyncio as _asyncio

        async def _go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url=prefix,
            ) as cli:
                return await cli.request(method, rel, **kwargs)

        return _asyncio.run(_go())

    _real_request = httpx.request
    monkeypatch.setattr(httpx, "request", _patched_request)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _patched_request("GET", url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _patched_request("POST", url, **kw))
    monkeypatch.setattr(httpx, "put", lambda url, **kw: _patched_request("PUT", url, **kw))
    monkeypatch.setattr(httpx, "delete", lambda url, **kw: _patched_request("DELETE", url, **kw))
    monkeypatch.setattr(httpx, "patch", lambda url, **kw: _patched_request("PATCH", url, **kw))
    monkeypatch.setattr(httpx, "head", lambda url, **kw: _patched_request("HEAD", url, **kw))
    monkeypatch.setattr(
        httpx, "options", lambda url, **kw: _patched_request("OPTIONS", url, **kw)
    )
    yield
    # The active-app dict is cleared per test by ``server_app``
    # fixture teardowns; no cleanup needed here.
