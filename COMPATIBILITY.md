# Compatibility matrix

A per-feature map of where fastapi_turbo sits against its stated compat target (FastAPI 0.136.0 + Starlette). `Full` means the feature is observably indistinguishable from upstream in user code. `Partial` means the surface exists but some sub-behaviour diverges. `Different-by-design` flags intentional deviations that aren't parity bugs.

Status: 3,125 / 3,129 FastAPI upstream tests pass under the `import fastapi_turbo` sys.modules shim. Sentry ASGI integration: 33/33. Sentry FastAPI integration: 54/56. Own suite: 699 tests (410 general + 22 WebSocket + 160 stress + 107 parity snapshots).

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
| `app.host("subdomain", sub_app)` | Full | Dispatched via a Python middleware; sub-app's routes are matched directly (no re-entry through its ASGI entry). |
| `redirect_slashes` | Full | |
| HEAD auto-handling from GET | Full | 405 + `Allow: <declared>` — matches upstream FastAPI byte-for-byte. |
| OPTIONS auto-generation for CORS | Full | True preflights (`Origin` + `Access-Control-Request-Method`) are handled by `CORSMiddleware`; bare OPTIONS on undeclared method returns 405 + `Allow: <declared>` — matches upstream. |
| 405 Method Not Allowed on wrong method | Full | |

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
| `FileResponse` with `Range: bytes=N-M` / `N-` / `-N` | Full | Returns 206 Partial Content with `Content-Range`; 416 on unsatisfiable; 200 on malformed. |
| Multi-range responses (`bytes=0-0,-1` → `multipart/byteranges`) | Full | 206 with `multipart/byteranges; boundary=…`; per-part `Content-Type` + `Content-Range`; closing boundary; `Content-Length` set. DoS-capped: ≤ 16 ranges and sum-of-lengths ≤ 2× file size; hostile headers fall back to the full body rather than amplify. |
| `ORJSONResponse`, `UJSONResponse` | Full | When the optional dep is installed. |
| SSE: `EventSourceResponse`, `ServerSentEvent`, `format_sse_event` | Full | |
| `BackgroundTasks` single-task and multi-task | Full | |
| Request scalar / body returns (str, int, float, None, dict, list, dataclass, `BaseModel`) | Full | Top-level strings now produce RFC 8259-compliant JSON (control chars escaped). |
| `dataclass` / `TypedDict` / `msgspec.Struct` as response model | Full | `dict` / `list` / Pydantic / generic aliases / `@dataclass` / `TypedDict` all pass through Pydantic's `TypeAdapter` — filter, serialise and OpenAPI-schema the same way upstream does. `msgspec.Struct` is rejected at decoration time (matches upstream — Pydantic can't adapt it). |
| `max_request_size` on `FastAPI(...)` | Full | Enforced by Tower `RequestBodyLimitLayer`; the router no longer imposes a hidden 10 MiB cap. |

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
| `SentryAsgiMiddleware(app)` legacy wrap | Partial | Works via `TestClient`'s ASGI-transport fallback. Active-thread profiling (`profiles_sample_rate=1.0`) is not wired across our tokio→httpx→asyncio boundaries. |
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
| `AsyncClient` re-export | Full | `from fastapi.testclient import AsyncClient, ASGITransport` works. Dispatch runs **entirely in-process** — no loopback socket needed — covering: path match (+ `{name:path}` converter), query + JSON body + Pydantic validation, `Request` / `Response` / `BackgroundTasks` injection, `Depends(...)` (simple, nested, async, yield-with-teardown, `dependency_overrides`), `Security(...)` with `SecurityScopes` accumulation across the chain, `Form(...)` / `File(...)` / `UploadFile`, `StreamingResponse` / SSE (sync + async body iterators chunked via `more_body=True`), `response_model` filtering / `exclude_unset` / `by_alias` / `include` / `exclude`, `app.mount("/sub", subapp)` recursion, raw-ASGI `add_middleware(MW)` chains (LIFO composition), `@app.middleware("http")` functions, and WebSocket endpoints (`@app.websocket("/ws")` with path params). Duplicate request/response headers survive round-trip. |

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

- **Active-thread-id profiling under manual `SentryAsgiMiddleware(app)` wrap** — `profiles_sample_rate=1.0` requires thread-ident alignment across our tokio→httpx→asyncio hops. Not wired. Use the `integrations=[FastApiIntegration()]` pattern instead — that path gives full profiling.
- **HTTP/3 + QUIC** — stack is HTTP/1.1 + HTTP/2 via Axum.
- **Free-threaded Python (3.13t / 3.14t)** — works but not performance-tuned.
- **In-process ASGI dispatch** — HTTP + WebSocket scopes now dispatch entirely in-process (path match + query + JSON/form/multipart bodies + `Depends` graph + `@app.middleware('http')` + raw-ASGI `add_middleware` chains + mounts). The loopback proxy remains as a final fallback for the very-rare combinations that fall outside the in-process surface, so existing uvicorn-backed deployments aren't affected.

## How to run the regression suites

```bash
# Our own suite (every test exercises the sys.modules shim)
source /Users/venky/tech/fastapi_turbo_env/bin/activate
pytest tests/ -q

# FastAPI 0.136.0 upstream against the shim
cd /tmp/fastapi_upstream && pytest tests/ -q -p no:cacheprovider

# Sentry SDK integration tests (ASGI + FastAPI)
cd /tmp/sentry-python
pytest tests/integrations/fastapi/test_fastapi.py \
       tests/integrations/asgi/test_asgi.py \
       -p no:cacheprovider --no-cov --tb=no -q -o addopts=
```

## Reporting divergence

If you hit something that behaves differently from upstream FastAPI and isn't marked `Different-by-design` or `Partial` above, it's a parity bug — please file with a minimal reproducer.
