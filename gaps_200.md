# FastAPI Compatibility Gaps (Top 200 Repos Audit)

## Summary
- Repos analyzed: 205+
- Unique imports tested: 78
- Pass: 75 / Fail: 3
- **Pass rate: 96.2%**
- All Tier 1 (18/18) and Tier 2 (17/17) imports pass

## Import Failures

| Import | Tier | Used by (count) | Error | Fix Effort |
|--------|------|-----------------|-------|------------|
| `from fastapi.routing import Mount` | 3 | 1 (Airflow) | `cannot import name 'Mount' from 'fastapi.routing'` | Trivial: already in starlette shim, just missing from fastapi routing shim |
| `import fastapi._compat` | 3 | 1 (fastapi-jsonrpc) | `No module named 'fastapi._compat'` | Trivial: `_compat_shim.py` exists but not registered in sys.modules |
| `from starlette.routing import compile_path` | Starlette | 1 | `cannot import name 'compile_path' from 'starlette.routing'` | Easy: add regex path compiler to starlette routing shim |

## Failure Details

### 1. `fastapi.routing.Mount` (Airflow)

**What**: Airflow imports `Mount` from `fastapi.routing` to compose sub-applications.

**Root cause**: The fastapi shim's routing module exposes `APIRouter`, `APIRoute`, and `APIWebSocketRoute` but not `Mount`. However, the starlette shim already has `Mount` registered at `starlette.routing.Mount` (via `_starlette_compat.Mount`).

**Fix**: Add one line to `fastapi_shim.py`:
```python
fastapi_routing.Mount = _sc.Mount
```

### 2. `fastapi._compat` (fastapi-jsonrpc)

**What**: fastapi-jsonrpc imports `ModelField` and `Undefined` from `fastapi._compat`, which is FastAPI's Pydantic v1/v2 bridge module.

**Root cause**: The file `python/fastapi_rs/_compat_shim.py` exists with `ModelNameMap`, `Undefined`, and model helpers, but it is never registered in `sys.modules` as `fastapi._compat` by the compat shim.

**Fix**: Register in `fastapi_shim.py`:
```python
import fastapi_rs._compat_shim as _compat_impl
fastapi_compat = _mod("fastapi._compat")
# Copy all public attributes
for attr in dir(_compat_impl):
    if not attr.startswith("__"):
        setattr(fastapi_compat, attr, getattr(_compat_impl, attr))
modules["fastapi._compat"] = fastapi_compat
```

### 3. `starlette.routing.compile_path` (1 repo)

**What**: `compile_path` is a Starlette utility that converts a path template (e.g., `/users/{id}`) into a regex pattern and extracts parameter converters. Used by projects doing custom routing.

**Root cause**: Not implemented in the starlette routing shim.

**Fix**: Add to `starlette_shim.py`:
```python
import re
def compile_path(path: str):
    path_regex = "^"
    path_format = ""
    duplicated_params = set()
    idx = 0
    param_convertors = {}
    for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?\}", path):
        param_name = match.group(1)
        ...  # standard Starlette implementation
    ...
    return re.compile(path_regex), path_format, param_convertors
starlette_routing.compile_path = compile_path
```

## Deep Internal Gaps (Ecosystem Plugins)

These are NOT covered by the import test because they require functional stubs, not just importability. They are documented for awareness.

| Internal Import | Used by | Status | Notes |
|-----------------|---------|--------|-------|
| `fastapi.dependencies.utils.get_typed_signature` | fastapi-cache | Stub only | Returns `Dependant()` placeholder, not functional |
| `fastapi.dependencies.utils.get_parameterless_sub_dependant` | fastapi-pagination | Missing | Not even stubbed |
| `fastapi.dependencies.utils.get_body_field` | fastapi-pagination | Missing | Not stubbed |
| `fastapi.dependencies.utils._should_embed_body_fields` | fastapi-jsonrpc | Missing | Private API, low priority |
| `fastapi.routing.request_response` | fastapi-pagination | Missing | Internal route wrapper |
| `fastapi.routing.serialize_response` | fastapi-jsonrpc | Missing | Response serialization helper |
| `fastapi.routing._merge_lifespan_context` | tortoise-orm | Missing | Private lifespan merger |
| `fastapi._compat.ModelField` | fastapi-jsonrpc | Missing | Pydantic v1/v2 field abstraction |

## Behavioral Gaps (Not Tested by Import Check)

These are patterns where the import succeeds but runtime behavior may differ.

| Pattern | Difference | Used by |
|---------|------------|---------|
| `BaseHTTPMiddleware` subclass | Shim passes through but fastapi-rs uses Tower middleware natively; custom middleware classes using `dispatch(request, call_next)` may not intercept all requests | 8+ repos |
| `app.exception_handler(RequestValidationError)` | Custom exception handlers registered but may not override Rust-side 422 generation | 10+ repos |
| `StaticFiles` mount | Delegates to starlette; requires starlette installed as runtime dep | 8+ repos |
| `SessionMiddleware` | Delegates to starlette; cookie-based sessions work but may have timing differences | 7+ repos |
| `Jinja2Templates` | Requires jinja2 installed; template rendering is pure Python | 5+ repos |
| `WSGIMiddleware` | WSGI app wrapping; requires starlette runtime | 1 repo (Airflow) |
| `OAuth2PasswordRequestForm` as form dependency | Form body parsing in DI differs from FastAPI's `solve_dependencies` | 5+ repos |

## Priority Fix Recommendations

1. **`fastapi.routing.Mount`** -- One-line fix, used by Airflow (major project)
2. **`fastapi._compat`** -- Small fix, enables fastapi-jsonrpc ecosystem
3. **`starlette.routing.compile_path`** -- Low priority, only 1 repo uses it
