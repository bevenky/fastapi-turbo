# FastAPI Compatibility Gaps

Comprehensive audit against FastAPI 0.136.0 + analysis of 27 top real-world apps
(vLLM, SGLang, Open WebUI, LangServe, Gradio, Prefect, AutoGPT, FastAPI-Users, etc.)

## Status Summary

- **Covered**: ~220 symbols, 71/71 functional tests, 403 pytest, 3 audit suites green
- **Remaining gaps**: 85 items organized below
- **Apps analyzed**: 27 projects totaling ~500k+ GitHub stars

---

## Phase 1: P0 — Breaks Real-World Apps (19 items)

These are used by 10+ of the top 27 apps. Must fix first.

### 1.1 SSE Module (used by vLLM, SGLang, LangServe, LiteLLM, LangFlow, Gradio, Pipelines)
- [ ] `fastapi.sse` module — EventSourceResponse, ServerSentEvent, format_sse_event, KEEPALIVE_COMMENT
- [ ] `from fastapi.responses import EventSourceResponse` — must work

### 1.2 FastAPI.__init__ (used by ALL apps)
- [ ] `summary` param — not stored, missing from OpenAPI `info.summary`
- [ ] `openapi_external_docs` naming — we use `external_docs`, FastAPI uses `openapi_external_docs`
- [ ] `app.state` type — SimpleNamespace vs starlette State (isinstance checks fail)

### 1.3 Security Sub-modules (used by FastAPI-Users, Gradio, Template, Chainlit, AutoGPT)
- [ ] `from fastapi.security.oauth2 import OAuth2PasswordBearer` — sub-modules not registered
- [ ] `from fastapi.security.http import HTTPBearer` — fails
- [ ] `from fastapi.security.api_key import APIKeyHeader` — fails
- [ ] `SecurityBase` base class missing
- [ ] `get_authorization_scheme_param()` utility missing

### 1.4 OpenAPI Generation (used by Prefect, Template, Open WebUI, vLLM)
- [ ] `info.summary` not emitted
- [ ] 422 validation error response not auto-added to endpoints
- [ ] Response model schemas inline, not `$ref` to `components/schemas`
- [ ] `separate_input_output_schemas` accepted but behavior not implemented

### 1.5 Exception Handling (used by vLLM, SGLang, Prefect, Open WebUI, SlowAPI)
- [ ] `http_exception_handler` doesn't check `is_body_allowed_for_status_code`
- [ ] `websocket_request_validation_exception_handler` missing
- [ ] `ValidationException` base class missing

### 1.6 Sub-Application Mounting (used by Open WebUI, Mealie, Chainlit, SQLAdmin, Gradio, Prefect)
- [ ] `app.mount("/v2", sub_app)` where sub_app is another FastAPI — verify works
- [ ] `StaticFiles(html=True)` SPA mode — verify works

---

## Phase 2: P1 — Important for Compatibility (28 items)

Used by 5-10 of the top apps.

### 2.1 APIRouter Missing Methods (used by Strawberry, SQLAdmin, LangServe)
- [ ] `router.on_event("startup")` / `add_event_handler()`
- [ ] `router.mount("/sub", sub_app)`
- [ ] `router.url_path_for("route_name")`
- [ ] `router.websocket_route("/ws")`
- [ ] `router.route("/path")` generic decorator
- [ ] `router.add_route(path, endpoint)`
- [ ] `router.add_api_websocket_route(path, endpoint)`
- [ ] `router.host("example.com", app=sub_app)`

### 2.2 APIRouter Missing Init Params (used by Strawberry, Mealie)
- [ ] `redirect_slashes` on APIRouter
- [ ] `on_startup` / `on_shutdown` on APIRouter
- [ ] `lifespan` on APIRouter
- [ ] `dependency_overrides_provider`
- [ ] `default` fallback handler
- [ ] `strict_content_type`

### 2.3 FastAPI Missing Methods (used by Prefect, Template)
- [ ] `app.setup()` — no-op stub
- [ ] `app.build_middleware_stack()` — no-op stub
- [ ] `app.host()` — host-based routing
- [ ] `app.websocket_route()` — Starlette-style

### 2.4 Param Classes (used by AutoGPT, LangFlow)
- [ ] `default_factory` — Pydantic v2 lazy defaults
- [ ] `validation_alias` / `serialization_alias` / `alias_priority`
- [ ] `discriminator` — union discriminator
- [ ] `strict` — Pydantic strict mode
- [ ] `multiple_of`, `allow_inf_nan`, `max_digits`, `decimal_places`

### 2.5 Exception Classes
- [ ] `DependencyScopeError`
- [ ] `PydanticV1NotSupportedError`
- [ ] `FastAPIDeprecationWarning`
- [ ] `RequestErrorModel` / `WebSocketErrorModel`
- [ ] `RequestValidationError(endpoint_ctx=)` param

### 2.6 Other
- [ ] `fastapi.openapi.constants` — REF_PREFIX, REF_TEMPLATE, METHODS_WITH_BODY
- [ ] `Depends(scope="request")` — caching scope control
- [ ] `run_until_first_complete` in fastapi.concurrency (not just starlette shim)

---

## Phase 3: P2 — Edge Cases / Rare Usage (38 items)

Used by <5 apps or internal-only.

### 3.1 OpenAPI Models (used by Strawberry)
- [ ] `fastapi.openapi.models` — 40+ Pydantic models (Info, Contact, License, Server, PathItem, etc.)

### 3.2 OpenAPI Docs (used by Open WebUI, Prefect)
- [ ] `get_swagger_ui_html()` — full production HTML, not stub
- [ ] `swagger_ui_default_parameters` dict
- [ ] `get_swagger_ui_oauth2_redirect_html()`

### 3.3 Starlette Internals
- [ ] `BaseRoute` abstract class (used by Strawberry)
- [ ] `Match` enum, `NoMatchFound` exception
- [ ] `WebSocketClose` ASGI message
- [ ] `ServerErrorMiddleware`, `ExceptionMiddleware`
- [ ] `SessionMiddleware` functional verification (used by Open WebUI, Mealie)
- [ ] `AsyncExitStackMiddleware`
- [ ] `starlette.config.Config`

### 3.4 FastAPI Internals
- [ ] `fastapi.dependencies.models.Dependant` — full dataclass
- [ ] `fastapi.dependencies.utils` — solve_dependencies, analyze_param, etc.
- [ ] `fastapi.utils` — deep_dict_update, is_body_allowed_for_status_code, etc.
- [ ] `fastapi._compat` — Pydantic v1/v2 compat
- [ ] `fastapi.cli` / `fastapi.__main__`
- [ ] `fastapi.types` — DependencyCacheKey, ModelNameMap

### 3.5 Behavioral Differences
- [ ] `deprecated` as string (OpenAPI 3.1)
- [ ] `UploadFile.content_type` should be @property
- [ ] `separate_input_output_schemas` behavior
- [ ] OpenAPI $ref deduplication
- [ ] OpenAPI auto-422 response schema

### 3.6 Real-World App Patterns
- [ ] SQLModel integration (session deps, auto table creation)
- [ ] FastAPI-Users router factory pattern
- [ ] slowapi rate limiting (app.state + custom exception handler)
- [ ] fastapi-pagination (add_pagination, Page models)
- [ ] SPA static file serving (custom SPAStaticFiles class)
- [ ] Socket.IO ASGI mounting (used by Open WebUI, Chainlit)
- [ ] WSGIMiddleware for Flask sub-apps (used by Airflow)
- [ ] Brotli/compression middleware (used by Gradio, Open WebUI)
- [ ] Custom OpenAPI schema modification via `app.openapi_schema`
- [ ] `starlette.datastructures.MutableHeaders` direct import (used by Open WebUI)
- [ ] `from starlette.exceptions import HTTPException as StarletteHTTPException` (used by Open WebUI)
- [ ] `generate_unique_id` for custom operation IDs (used by Template, Strawberry)

---

## Top 27 Apps Pattern Frequency

| Pattern | Apps Using | Priority |
|---------|-----------|----------|
| FastAPI + HTTPException + Request + Depends | 25+ | Covered |
| CORSMiddleware | 18+ | Covered |
| StreamingResponse (SSE) | 16+ | Covered |
| Lifespan context manager | 15+ | Covered |
| include_router(prefix, tags, deps) | 20+ | Covered |
| UploadFile + File + Form | 14+ | Covered |
| BackgroundTasks | 12+ | Covered |
| Custom exception handlers | 12+ | Covered |
| OAuth2/Security | 10+ | Covered |
| BaseHTTPMiddleware | 8+ | Covered |
| StaticFiles mount | 8+ | Covered |
| Sub-app mounting | 8+ | Verify |
| SessionMiddleware | 7+ | Verify |
| GZipMiddleware | 7+ | Covered |
| sse-starlette EventSourceResponse | 8+ | Covered (3rd party) |
| WebSocket | 8+ | Covered |
| jsonable_encoder | 7+ | Covered |
| run_in_threadpool | 6+ | Covered |
| response_model | 7+ | Covered |
| Jinja2Templates | 5+ | Covered |

---

## Previously Fixed (45 items)

[x] include_router(dependencies=...), TemplateResponse dual signature, app.routes,
add_api_route, ASGI __call__, FieldInfo params, Request.form() FormData,
FileResponse content_disposition_type, on_startup/on_shutdown in __init__,
middleware= list, HTTPBasicCredentials BaseModel, security __call__ Request,
UploadFile Pydantic integration, swagger_ui_* params, generate_unique_id_function,
separate_input_output_schemas param, callbacks, deprecated/include_in_schema on app,
openapi_prefix, strict_content_type, OAuth2PasswordRequestFormStrict, OAuth2 base,
route_class, WebSocket.state, Request.is_disconnected, pattern alias, Form.media_type,
openapi_examples, Jinja2Templates improvements, MutableHeaders, listen_for_disconnect,
FileResponse.stat_result, 17 compat shim gaps, Enum coercion, BaseHTTPMiddleware,
sse_starlette compat, psycopg3 autocommit, Decimal serialization, asyncpg + psycopg3,
CORS+WS coexistence, WS param naming, root_path semantics, RequestValidationError.errors(),
api_route() multi-method
