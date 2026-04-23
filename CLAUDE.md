# fastapi-turbo Development Guide

## Commit Rules

- Do not include "Claude", "Anthropic", or "Co-Authored-By" in commit messages.

## Project Overview

fastapi-turbo is a drop-in replacement for FastAPI, powered by Rust Axum via PyO3. It maintains 100% FastAPI API compatibility while delivering near-Go/Rust performance.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Rust Core (src/)                                │
│  HTTP: Axum 0.8 + hyper + tokio                  │
│  Routing: matchit (radix tree)                   │
│  Middleware: Tower (CORS, compression)            │
│  WebSocket: axum::ws + crossbeam channels         │
│  JSON: serde_json + direct PyDict→JSON writer     │
│  DI: Pre-compiled resolution plans, executed in   │
│      Rust with topological ordering               │
├─────────────────────────────────────────────────┤
│  PyO3 Boundary (minimal crossings)               │
│  Sync: block_in_place → with_gil (1 GIL acq)     │
│  Async: coro.send(None) try-sync fast path        │
│  WS: ChannelAwaitable (custom Python awaitable    │
│      backed by crossbeam, zero asyncio overhead)  │
├─────────────────────────────────────────────────┤
│  Python Layer (python/fastapi_turbo/)               │
│  FastAPI-identical API surface                    │
│  Compat shims: from fastapi import ... works      │
│  Introspection: inspect.signature at startup      │
│  OpenAPI: generated once at startup               │
└─────────────────────────────────────────────────┘
```

## Build & Test

```bash
# Activate venv (GIL-enabled Python 3.14 recommended)
source /Users/venky/tech/jamun_env/bin/activate

# Build (release for benchmarks, dev for iteration)
PATH="$HOME/.cargo/bin:$PATH" maturin develop          # debug, ~5s incremental
PATH="$HOME/.cargo/bin:$PATH" maturin develop --release  # optimized, ~8s

# Test (skip WS tests for speed — they need server startup)
pytest tests/ -x -q --ignore=tests/test_websocket.py   # ~6s, 124 tests
pytest tests/ -x -q                                      # ~50s, 128 tests

# Benchmark
./target/release/fastapi-turbo-bench 127.0.0.1 PORT /path N WARMUP [METHOD] [BODY] [CONTENT_TYPE]
```

## Key Design Decisions

- **PyO3 0.25** — not 0.28, because 0.28 requires API migration (with_gil→attach, etc.) with no perf gain
- **orjson is optional** — removed from required deps for free-threaded Python compatibility
- **Response serialization**: Direct PyDict→JSON writer in Rust (bypasses serde_json::Value intermediate)
- **Async deps**: Wrapped in sync callers at startup via `_make_sync_wrapper` — drives coroutines via `coro.send(None)` in Python, avoiding PyO3 coroutine protocol overhead
- **WS receive**: `ChannelAwaitable` — custom `#[pyclass]` implementing `__await__`/`__next__`, blocks on crossbeam channel with GIL released
- **Body validation**: Uses `__pydantic_validator__.validate_json(bytes)` directly — cached at startup, skips Python wrapper overhead

## File Structure

```
src/
├── lib.rs              # PyO3 module entry point
├── server.rs           # Axum server, middleware, OpenAPI endpoints
├── router.rs           # Route building, request handler (HOT PATH)
├── handler_bridge.rs   # Sync/async Python handler calls, event loop
├── responses.rs        # Python→HTTP response conversion, direct JSON writer
├── websocket.rs        # WS bridge, ChannelAwaitable, pure Rust echo
├── streaming.rs        # StreamingResponse bridge
└── config.rs           # ServerConfig

python/fastapi_turbo/
├── __init__.py         # Public API + compat shim auto-install
├── applications.py     # FastAPI class, compiled handler optimization
├── routing.py          # APIRouter, APIRoute
├── _introspect.py      # Function signature analysis
├── _resolution.py      # Dependency graph topological sort
├── _openapi.py         # OpenAPI 3.1 schema generation
├── _async_bridge.py    # Event loop scheduling helper
├── _ws_pipe_bridge.py  # (legacy) Pipe-based WS bridge
├── param_functions.py  # Query, Path, Header, Cookie, Body, Form, File
├── dependencies.py     # Depends class
├── security.py         # OAuth2, HTTPBearer, APIKey schemes
├── responses.py        # Response classes (JSON, HTML, Streaming, etc.)
├── requests.py         # Starlette-compatible Request wrapper
├── websockets.py       # WebSocket wrapper with ChannelAwaitable
├── background.py       # BackgroundTasks
├── encoders.py         # jsonable_encoder
├── status.py           # HTTP/WS status code constants
├── datastructures.py   # URL, Headers, QueryParams, State
├── concurrency.py      # run_in_threadpool
├── testclient.py       # TestClient (httpx, real HTTP)
├── middleware/          # CORS, GZip, TrustedHost, HTTPSRedirect
└── compat/             # sys.modules shims for fastapi.* and starlette.*
```

## Performance-Critical Code

The hot path for a request is in `src/router.rs::handle_request`:
1. Skip body read if no body params (flag pre-computed at startup)
2. Skip headers clone if no header/cookie params (flag pre-computed)
3. Sync handler with no deps: single `Python::with_gil` (1 GIL acquisition)
4. Async/deps handler: single `block_in_place` → `with_gil` with try-sync for async
5. Response: `write_dict_json` writes JSON directly from PyDict (no serde_json::Value)

Do NOT add unnecessary `Python::with_gil()` calls. Every GIL acquisition costs ~2μs.

## Common Tasks

**Adding a new parameter type**: Add to `param_functions.py`, update `_introspect.py` classification, update `router.rs` extraction.

**Adding middleware**: Add Tower layer config to `server.rs::MiddlewareConfig` enum, add Python wrapper in `middleware/`, update `applications.py::_build_middleware_config`.

**Changing response handling**: Edit `responses.rs::py_to_response`. Dict path is checked FIRST for performance.
