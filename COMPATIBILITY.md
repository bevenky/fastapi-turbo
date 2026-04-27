# Compatibility matrix

A per-feature map of where fastapi_turbo sits against its stated compat target (FastAPI 0.136.0 + Starlette). `Full` means the feature is observably indistinguishable from upstream in user code. `Partial` means the surface exists but some sub-behaviour diverges. `Different-by-design` flags intentional deviations that aren't parity bugs.

Status: **3,119 / 3,119 FastAPI upstream tests pass** under the `import fastapi_turbo` sys.modules shim via the canonical gate (`scripts/run_external_compat_gates.sh fastapi`) — zero failed, zero skipped, zero xfailed. The gate deselects 10 upstream-FastAPI tests that aren't ours to fix (`tests/benchmarks/test_general_performance.py` opt-in via `--codspeed`, `tests/test_pydantic_v1_error.py` skipped by upstream on py3.14+, and 4 cases in `tests/test_tutorial/test_query_params_str_validations/test_tutorial006c.py` xfailed by upstream per [fastapi/fastapi#12419](https://github.com/fastapi/fastapi/issues/12419) — upstream's own decisions, not compat regressions). The gate script also bails out with a `maturin develop` instruction when the loaded `_fastapi_turbo_core.*.so` is older than any `src/*.rs` source — fixes the recurring auditor reports of 888 failures (correlated with stale pre-R34 builds). Threshold regression at `tests/stress/test_r36_regressions.py::test_upstream_fastapi_gate_passes_canonical_threshold` calls the canonical script and fails loudly below 3000 pass / non-zero failed/skipped/xfailed. Sentry ASGI integration: 33/33. Sentry FastAPI integration: 89/89. Own suite: 1010 tests (410 general + 22 WebSocket + 471 stress + 107 parity snapshots).

**Test suite under different environments:**

* **Normal dev box / CI** (loopback bind allowed): all 1010 tests run (1 conditional skip when Starlette wasn't pre-imported), 0 failed.
* **Sandbox / restricted CI** (`socket.bind('127.0.0.1', 0)` denied with `PermissionError` in the pytest process): `tests/conftest.py` detects this at session start via a one-shot bind probe and sets `LOOPBACK_DENIED = True`. Tests that exercise the in-process / ASGI dispatch path run cleanly via a sandbox-aware `server_app` fixture (exec's the app in-process, routes `httpx.*` through `ASGITransport`); tests that genuinely need a real loopback port are skipped via `@pytest.mark.requires_loopback`.
  - **Force-override env vars** (audit / CI use): set `FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1` to skip `requires_loopback` tests even on a dev box that *can* bind, or `FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED=1` to run them anyway in an env where probe bind fails but the real subprocess server might still succeed.
  - **Counts at the R37 watermark**, measured on macOS Apple Silicon:
    1. *True sandbox + FORCE env var* — every bind raises AND
       `FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1`: 848 pass, 163 skipped.
       The conftest collection hooks (suite-level + parity-level)
       both honour the FORCE env var (R33), so this scenario also
       covers a dev box where the auditor wants the bucket-#1
       numbers without having to actually deny bind at the kernel.
    2. *Forced-fail bind only* — monkey-patched `socket.socket.bind`
       to raise `PermissionError`, no env var: 848 pass, 163 skipped
       (same as #1 — the suite-level probe hits the patched bind
       first, propagates `LOOPBACK_DENIED=True`, parity collection
       hook also detects it).
    3. *Bind works, no env var* (normal dev box): 1009 pass, 1 skipped
       — full coverage. This is the "happy path" that CI / release
       runners hit.
  - The FORCE env var IS sufficient to produce bucket #1 numbers on
    a dev box that can bind. The R33 audit caught a case where the
    parity conftest didn't honour the env var, so FORCE on a dev
    box ran parity normally and produced 924 pass / 55 skipped (a
    third bucket that no longer exists). Both legitimate flavours
    surface **0 failed, 0 errors** — the skip-count delta reflects
    what the conftest layers actually denied, not a regression.

> ### Release readiness — **a real-loopback CI run is REQUIRED before shipping**
>
> Sandbox mode is "valid run, partial coverage" — it validates the ASGI / Python dispatch path and the in-process parity surface, but the skipped tests are precisely the ones that exercise the parts of the system most likely to regress on real hardware:
>
> 1. **107 parity-runner tests** (`tests/parity/`) — drive both upstream FastAPI AND turbo as subprocess servers on real loopback ports and diff the responses. The whole compatibility claim against FastAPI 0.136.0 hangs off these.
> 2. **22 `test_websocket.py`** — drive `websockets.sync.client.connect` against a real subprocess WS server. Catches real-WS-protocol regressions (handshake, ping/pong, close codes over the wire) that the in-process WS path can't reproduce.
> 3. **9 bench-CLI tests** — exercise the `fastapi-turbo-bench` Rust binary against a live server. Catches regressions in the Rust `bench-app` integration and CLI argument compatibility shim.
> 4. **4 `test_public_bind_warning.py`** — verify the public-bind DoS warning fires only when the server actually starts on a public address.
> 5. **4 `test_testclient_lifecycle.py`** — assert on `cli._port` / `TestClient._app_servers` cache state (real-server-only invariants).
> 6. **4 `test_r23_regressions.py`** — Rust-path closed-file I/O guards on `PyUploadFile` / `PySyncFile` (the in-process tests in `test_r22_regressions.py` exercise the Python `BytesIO` path, not the Rust source these tests cover).
> 7. **2 `test_middleware.py`** — Tower-bound HTTPSRedirect's `X-Forwarded-Proto` handling, TrustedHost with hard-coded `127.0.0.1` host.
> 8. **1 `test_concurrent_clients.py`** — 100 actual concurrent socket connections to validate the request-pipeline doesn't deadlock under load.
>
> These cover the Rust + Tower + socket pipeline. Sandbox green is necessary but not sufficient. **Always run the full suite on a normal CI runner (loopback unrestricted) before tagging a release.**

## Routing

| Feature | Status | Notes |
|---|---|---|
| `app.get/post/put/patch/delete/head/options/trace` | Full | |
| `app.api_route(..., methods=[...])` | Full | |
| `APIRouter` + `include_router(prefix=, tags=, dependencies=)` | Full | |
| Path params with `{name}` and `{name:path}` | Full | |
| `typing.Annotated[T, Query()]` / `Path()` / `Header()` / `Cookie()` / `Body()` / `Form()` / `File()` | Full | |
| `response_model=` (filtering, aliases, `model_validate(obj)`) | Full | |
| `status_code=` / `tags=` / `summary=` / `description=` / `response_description=` | Full | |
| `app.mount("/sub", sub_app)` for FastAPI / StaticFiles / ASGI | Full | |
| `app.host("subdomain", sub_app)` | Full | Dispatched in-process by the ASGI entry: ``_asgi_dispatch_in_process`` checks the registered hosts BEFORE route match and recurses into the matching sub-app's ``__call__`` (R28). Works under raw ``httpx.ASGITransport(app=app)`` / serverless / sandbox runs without binding a loopback socket. Sub-app keeps its own route table, lifespan, and middleware chain. |
| `redirect_slashes` | Full | |
| HEAD auto-handling from GET | Full | 405 + `Allow: <declared>` — matches upstream FastAPI byte-for-byte. |
| OPTIONS auto-generation for CORS | Full | True preflights (`Origin` + `Access-Control-Request-Method`) are handled by `CORSMiddleware`; bare OPTIONS on undeclared method returns 405 + `Allow: <declared>` — matches upstream. |
| 405 Method Not Allowed on wrong method | Full | |
| 405 `Allow` header on overlapping routes (literal vs param) | Full | When ``@app.get('/items/{id}')`` and ``@app.post('/items/special')`` are both registered, OPTIONS /items/special on upstream FastAPI returns ``Allow: GET`` (first registered route's methods). Both the in-process / TestClient ASGI fallback AND the Rust server now return the first-match-wins Allow header — the Rust router post-processes its per-path Allow values via a registration-order pattern walk so matchit's most-specific-literal selection no longer leaks into the 405 response (R27). |

## Dependencies

| Feature | Status | Notes |
|---|---|---|
| `Depends()` with nested chains, caching | Full | |
| async deps | Full | |
| yield dependencies (generator / async generator) with teardown | Full | Teardown runs in reverse (LIFO) after middleware unwinds. On handler exception, `gen.athrow(exc)` is driven so `except` clauses in the dep observe the error. |
| `dependency_overrides` | Full | Checked at runtime. |
| `Security()` + `SecurityScopes` | Full | |
| `Depends(scope="request")` / `Depends(scope="function")` | Full | `.scope` attribute preserved for introspection; `yield`-dep teardown ordering matches upstream byte-for-byte under the TestClient across both scope values. |
| `Depends(use_cache=False)` | Full | |

## Request / Response

| Feature | Status | Notes |
|---|---|---|
| Pydantic v2 body validation | Full | Uses `__pydantic_validator__.validate_json(bytes)` directly. |
| `Form`, `File`, `UploadFile`, multipart | Full | Rust multipart parser; `UploadFile.read/seek/write/close`. |
| `Response`, `JSONResponse`, `HTMLResponse`, `PlainTextResponse`, `RedirectResponse`, `StreamingResponse`, `FileResponse` | Full | |
| `FileResponse` with `Range: bytes=N-M` / `N-` / `-N` | Full | 206 Partial Content with `Content-Range`; 416 on out-of-bounds; 400 on malformed (non-`bytes` unit, reversed `5-3`, no parseable sub-ranges). Validation order matches Starlette 1.0 (bounds check before reversed check). 400 / 416 bodies + headers match upstream byte-for-byte. |
| Multi-range responses (`bytes=0-0,-1` → `multipart/byteranges`) | Full | 206 with `multipart/byteranges; boundary=…`; CRLF wire framing; per-part `Content-Type` + `Content-Range` (Content-Type echoes the response's content-type, including `; charset=utf-8` for textual files); closing `--{boundary}--`; `Content-Length` precomputed. Overlapping/adjacent sub-ranges coalesce before deciding single vs multipart, matching Starlette. No range-count cap — matches upstream's behaviour for download accelerators / media segmenters. |
| `ORJSONResponse`, `UJSONResponse` | Full | When the optional dep is installed. |
| SSE: `EventSourceResponse`, `ServerSentEvent`, `format_sse_event` | Full | |
| `BackgroundTasks` single-task and multi-task | Full | |
| Request scalar / body returns (str, int, float, None, dict, list, dataclass, `BaseModel`) | Full | Top-level strings now produce RFC 8259-compliant JSON (control chars escaped). |
| `dataclass` / `TypedDict` / `msgspec.Struct` as response model | Full | `dict` / `list` / Pydantic / generic aliases / `@dataclass` / `TypedDict` all pass through Pydantic's `TypeAdapter` — filter, serialise and OpenAPI-schema the same way upstream does. `msgspec.Struct` is rejected at decoration time (matches upstream — Pydantic can't adapt it). |
| `max_request_size` on `FastAPI(...)` | Full | Enforced by Tower `RequestBodyLimitLayer`; the router no longer imposes a hidden 10 MiB cap. **Expected client-side log line:** when the client streams a body larger than the cap, the server rejects mid-stream and TCP drops while the client is still writing — httpcore logs `send_request_body.failed` with `BrokenPipeError` / `ConnectionResetError` for that iteration even though the client successfully observes the 413 response. This matches nginx / axum / most production servers that early-reject. The R31 regression in `tests/stress/test_r31_regressions.py` accepts both outcomes (413 status OR an early-reject httpx exception family) so the line is a documented part of the contract, not a regression. |

## Middleware

| Feature | Status | Notes |
|---|---|---|
| `app.add_middleware(CORSMiddleware)` | Full | Tower-backed (~0.3 µs per request). |
| `GZipMiddleware` | Full | Tower-backed. |
| `TrustedHostMiddleware` | Full | Python-ASGI (not Tower) so outer Sentry can observe host-rejected 400s. |
| `HTTPSRedirectMiddleware` | Full | Tower-backed. |
| `BaseHTTPMiddleware` subclasses | Full | |
| Raw ASGI3 middleware classes | Full | Shim bridges `scope/receive/send` in/out of the per-handler chain. |
| Last-added-is-outermost ordering | Full | |
| `SessionMiddleware` | Full | |
| Lifespan scope through ASGI middleware | Full | |
| WebSocket scope through ASGI middleware | Full | |

## Sentry integrations

| Feature | Status | Notes |
|---|---|---|
| `app.add_middleware(SentryAsgiMiddleware)` | Full | Captures transactions + errors + request context end-to-end. |
| `sentry_sdk.init(integrations=[FastApiIntegration(), StarletteIntegration()])` | Full | Auto-installs `SentryAsgiMiddleware`; transaction naming refined to route/endpoint; `failed_request_status_codes` honoured; `http_methods_to_capture` propagated. |
| `SentryAsgiMiddleware(app)` legacy wrap | Full | The dispatcher invokes Sentry's `_set_transaction_name_and_source` inline when `FastApiIntegration` is loaded (R26), and `active_thread_id` propagates through the in-process loop (verified by the upstream `test_active_thread_id` cases passing). Run is gated in CI under the pinned Sentry-SDK version. |
| Transaction name when a middleware rejects early (e.g. TrustedHost returns 400) | Full | `transaction_style="endpoint"` → MW class qualname; `url` → full URL. |

## WebSocket

| Feature | Status | Notes |
|---|---|---|
| `@app.websocket("/ws")` declaration | Full | |
| `accept(subprotocol=…, headers=…)` | Full | `headers=` is a Starlette 0.27+ addition; accepted. |
| `send_text/send_bytes/send_json`, `receive_text/bytes/json` | Full | Custom `ChannelAwaitable` (Rust crossbeam channel + GIL release) — the `await` completes without asyncio scheduling. |
| `iter_text`, `iter_bytes`, `iter_json` | Full | |
| Client disconnect → `WebSocketDisconnect` | Full | |
| Custom close codes | Full | |
| Path params + query params + `Depends` in WS handlers | Full | |
| WebSocket state mutation across messages | Full | |
| Handler that `await`s real asyncio primitives (`asyncio.sleep(0.1)`, `asyncio.wait`) before `accept()` | Full | Re-dispatched to the shared async worker loop on `RuntimeError: no running event loop`. |
| Large frames (64 KiB+) | Full | |
| APIRouter-mounted WebSocket route | Full | |

## Exception handling

| Feature | Status | Notes |
|---|---|---|
| `HTTPException(status_code, detail, headers)` | Full | |
| `@app.exception_handler(HTTPException)` | Full | Request passed to the handler carries real `url.path` / `method` / `query_string` (previously a stub). |
| `RequestValidationError` | Full | |
| `WebSocketRequestValidationError` | Full | |
| `WebSocketException(code, reason)` | Full | |
| `startup` / `shutdown` events | Full | `on_event("startup"|"shutdown")` and `lifespan=` context manager. |

## OpenAPI

| Feature | Status | Notes |
|---|---|---|
| OpenAPI 3.1 schema | Full | Regenerated once at startup. |
| Swagger UI (`/docs`), ReDoc (`/redoc`) | Full | |
| `response_model`-derived schemas with `-Input`/`-Output` split | Full | |
| `callbacks`, `summary`, `description`, `tags`, `deprecated`, `operation_id` | Full | |
| `generate_unique_id_function` | Full | Honoured at app / router / route levels; duplicate `operation_id` emits `UserWarning`. Covered in `tests/stress/test_operation_id_and_unique_fn.py`. |
| Per-route `servers` / `external_docs` | Full | Both via `openapi_extra={'servers': …, 'externalDocs': …}` (upstream-compatible) and via our own `servers=` / `external_docs=` decorator kwargs. Covered in `tests/stress/test_per_route_openapi_extras.py`. |
| `webhooks=` app parameter + OpenAPI webhooks section | Full | `app.webhooks.post(...)` surfaces under the top-level `webhooks` key; `FastAPI(webhooks=router)` also accepted. Covered in `tests/stress/test_webhooks.py`. |

## Testing

| Feature | Status | Notes |
|---|---|---|
| `TestClient` (real-HTTP via httpx against a live server) | Full | |
| `TestClient.websocket_connect(...)` | Full | |
| `TestClient(raise_server_exceptions=True/False)` | Full | Non-HTTP exceptions captured server-side and re-raised in the test thread. |
| ASGI transport fallback (wrap with `SentryAsgiMiddleware(app)`, etc.) | Full | Detected via absence of `.run()` — falls back to an `httpx.AsyncClient` + `ASGITransport` with a sync facade. |
| `AsyncClient` re-export | Full | `from fastapi.testclient import AsyncClient, ASGITransport` works fully in-process — no loopback socket. A 13-case parity contract test (`tests/stress/test_asgi_in_process_parity_contract.py`) runs both upstream FastAPI and fastapi-turbo on the same endpoint and asserts equal status/body/headers, covering: 404 on unknown path, 405 + `Allow` on wrong method, HEAD on GET-only, OPTIONS non-preflight, `Header(...)` / `Cookie(...)` markers, missing-required `Query(...)` → 422, invalid Pydantic body → 422, bad path-param type coercion → 422, `Depends(...)` with inner query/header/cookie params, `response_model` validation failure (non-200), custom `@app.exception_handler(HTTPException)` override, custom user exception types. Additional coverage for `Request` / `Response` / `BackgroundTasks` injection, `Security(...)` + `SecurityScopes`, `Form(...)` / `File(...)` / `UploadFile`, `StreamingResponse` / SSE, `app.mount(...)` recursion, raw-ASGI middleware chains (LIFO), `@app.middleware("http")`, and WebSocket endpoints — each with its own test file. |

## Database / HTTP client helpers

| Feature | Status | Notes |
|---|---|---|
| `fastapi_turbo.db.create_pool(dsn, ...)` | Full | Opt-in helper — defaults to `autocommit=True` for ~46 µs saved per query. |
| `fastapi_turbo.db.create_redis()` | Full | Opt-in helper — enables hiredis + `decode_responses=True`. |
| `fastapi_turbo.http.Client` | Full | Httpx-compatible, Rust-reqwest-backed. |
| Global monkey-patch of `psycopg_pool.ConnectionPool.__init__` | **Removed** | Silent behavioural change for unrelated code; removed in the P0 audit pass. |

## Process

| Feature | Status | Notes |
|---|---|---|
| Worker loop timeout on slow async handlers | Full | Default `None` (no timeout — matches FastAPI); overridable via `FASTAPI_TURBO_WORKER_TIMEOUT` env or `FastAPI(worker_timeout=…)`. On timeout the underlying task is cancelled — cancellation is race-free against the worker-loop kickoff (even ``timeout=0`` prevents the coroutine from running). Multi-app isolation is full: every submit site carries per-app context — the Python layer threads ``app=…`` through `_make_sync_wrapper`, `_run_pending_teardowns`, lifespan, and background-tasks; the Rust layer threads `APP_INSTANCE` through `submit_to_async_worker`. |
| Streaming to a slow client | Full | `tx.blocking_send(...)` runs under `py.allow_threads` so the GIL is released during backpressure. |
| Shim `import fastapi` / `import starlette` | Full | Installed on `import fastapi_turbo`. Reports `fastapi.__version__ = "0.136.0"` (the compat target), not our own `0.1.0`. |

## Known limitations

- **HTTP/3 + QUIC** — stack is HTTP/1.1 + HTTP/2 via Axum.
- **Free-threaded Python (3.13t / 3.14t)** — works but not performance-tuned.
- **In-process ASGI dispatch** — HTTP + WebSocket scopes now dispatch entirely in-process (path match + query + JSON/form/multipart bodies + `Depends` graph + `@app.middleware('http')` + raw-ASGI `add_middleware` chains + mounts). The loopback proxy remains as a final fallback for the very-rare combinations that fall outside the in-process surface, so existing uvicorn-backed deployments aren't affected.

## How to run the regression suites

```bash
# Our own suite (every test exercises the sys.modules shim)
source /Users/venky/tech/fastapi_turbo_env/bin/activate
pytest tests/ -q

# External compat gates — same pinned tags + force-reset that
# CI / release.yml use, so a local run is bit-identical to the
# CI gate. Auditors should prefer this script over the manual
# pytest invocations below: it removes the "but is /tmp/... at
# the right tag?" question by force-resetting on every run.
./scripts/run_external_compat_gates.sh           # both gates
./scripts/run_external_compat_gates.sh fastapi   # upstream FastAPI 0.136.0 only
./scripts/run_external_compat_gates.sh sentry    # Sentry 2.42.0 only

# (Manual variant — only useful if you want to skip the force-reset
# and run against whatever tag is currently checked out at /tmp/...)
# FastAPI upstream against the shim:
cd /tmp/fastapi_upstream && pytest tests/ -q -p no:cacheprovider
# Sentry SDK integration tests (ASGI + FastAPI):
cd /tmp/sentry-python
pytest tests/integrations/fastapi/test_fastapi.py \
       tests/integrations/asgi/test_asgi.py \
       -p no:cacheprovider --no-cov --tb=no -q -o addopts=
```

## Reporting divergence

If you hit something that behaves differently from upstream FastAPI and isn't marked `Different-by-design` or `Partial` above, it's a parity bug — please file with a minimal reproducer.
