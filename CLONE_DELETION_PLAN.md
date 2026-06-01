# Clone Deletion Plan

Scoped via the `clone-deletion-scope` workflow (11 agents mapping every clone subsystem + the Rust↔Python boundary). Goal: delete the ~30K-line Python clone, keep the Rust door (`src/`), use real pip fastapi/starlette.


**Total deletable:** ~26K-27K of the ~30.6K-LOC clone (python/fastapi_turbo/*.py = 30,613 LOC + compat/ 1,609 + middleware/ 295). Cleanly deletable: applications.py group B+C dispatcher/overrides (~6.6K), _openapi.py (2909), _introspect.py (~1.8K net after relocating ~200 LOC of door glue), routing.py (1313), responses.py (753), _resolution.py (736), security.py (677), _route_helpers.py (~700 net), requests.py (588), datastructures.py (610), authentication.py (247), encoders.py (259), exceptions.py (~150), staticfiles.py (158), status.py (107), sse.py (98), background.py (80), param_functions.py markers (~270), dependencies.py (24), _ws_pipe_bridge.py (61), _compat_shim.py (172), _starlette_compat.py (624), fastapi_shim.py (1072), starlette_shim.py (473), middleware markers (~120). MUST KEEP (~4K, the irreducible Rust-door Python side): applications.py group A engine glue (_build_server_args/_collect_all_routes/_try_compile_handler/_adapter_route_info/RouteInfo+ParamInfo/multiworker/oneshot/proxy ~3.0K) + group D lifespan (~700), _introspect_from_real_fastapi.py (576, the replacement), _door_support.py (new, ~200 relocated), _async_worker.py (237), _async_bridge.py (13), websockets.py (502, Rust WS wrapper), _middleware_wrap.py (612, door kwargs->ASGI bridge), UploadFile (relocated), a thin compat re-pointer (~10 LOC), _sentry_compat.py (383, if Sentry parity kept).


## Critical path

The Rust door constructs six clone classes BY NAME and reads clone-private members off them — these are the irreducible ordering constraint. The door does NOT care how routes are introspected (ParamInfo/RouteInfo are Rust-owned pyclasses already adapter-fed from real fastapi route.dependant), so introspection/openapi/routing/params can pivot first. But NO clone CLASS the door instantiates can be deleted until its construction site is re-pointed.

The single hardest re-point — and the true gate — is responses.py. Clone `Response` (responses.py:123) is a STANDALONE class (no base) whose `raw_headers` is `list[tuple[str,str]]`. Real starlette uses `list[tuple[bytes,bytes]]`. Three Rust sites — router.rs:453/489 apply_injected_response, responses.rs:353/968 response_object_to_response + extract_response_headers — all do `extract::<(String,String)>()`, which SILENTLY SKIPS bytes tuples. So merely re-pointing responses.rs's 5 cached OnceLocks (responses.rs:34-49) + response_cls (router.rs:68) to real starlette would make handler-set cookies/headers vanish on app.run() with zero errors. The Rust header readers MUST accept (bytes,bytes)/Vec<u8> FIRST, then re-point, then delete clone responses.py. This is the asymmetry that bites: Request/HTTPConnection/BackgroundTasks already subclass real starlette, but Response does not.

Second door gate: router.rs:1190 hard-imports `_introspect._make_fa_body_validator` at EVERY route build. This anchor keeps _introspect.py alive even after the adapter is 100% default-on. The 4 door-runtime helpers (_make_fa_body_validator/_FABodyValidator/_TypeAdapterProxy + _get_type_name, which the adapter itself imports from _introspect — a circular blocker) must relocate to a kept door-support module before _introspect.py can die.

Therefore the critical path is: (1) relocate door glue out of _introspect [unblocks _introspect deletion + breaks adapter↔_introspect circularity], (2) make the adapter default-on by closing the type bridge — Response/UploadFile/WebSocket are the only remaining un-bridged types per _clone_framework_types(); bridging Response (subclass real starlette + bytes raw_headers) simultaneously closes the responses.rs re-point AND the adapter decline, (3) flip the compat shim from full sys.modules replacement to a thin re-point (fastapi.FastAPI -> accelerated only), (4) re-point the door's remaining 5 constructors (Request body via receive, BackgroundTasks .tasks/async __call__, SecurityScopes drop-in, WebSocket/WebSocketDisconnect wrapper kept), then (5) delete the big Python in-process dispatcher and clone modules in dependency order. Everything is gated on tests/parity/test_parity.py (the ONLY suite that exercises the real Rust door byte-for-byte) staying at 151 green.


## Recommended first step

Ship Phase 0 as the first PR, but lead with its single highest-confidence deletion to de-risk the pipeline: delete python/fastapi_turbo/_ws_pipe_bridge.py (verified dead — only importer is __init__.py:1 line; the live WS path is crossbeam channels in websocket.rs) and remove its __init__.py import, then repoint encoders.py to fastapi.encoders and delete it (AST-identical signature, zero Rust coupling, _json_default lives in responses.py so encoders is fully isolated). Gate on `python -m pytest tests/parity/test_parity.py -q` (151) staying green plus the full suite. This proves the delete-and-repoint workflow end-to-end with zero behavioral risk before touching any door constructor. The first STRUCTURAL PR (the real unblock) is Phase 2: relocate _make_fa_body_validator/_FABodyValidator/_TypeAdapterProxy/_get_type_name from _introspect.py into a new _door_support.py and re-point src/router.rs:1190 + _introspect_from_real_fastapi.py:51 + _openapi.py:1507 — this is the one change that severs the door's hard anchor on _introspect.py AND breaks the adapter↔_introspect circular import, unblocking everything downstream.


## Phases


### Phase 0 — Dead code + zero-risk constant/value-type drop-ins  _(risk=low, effort=low)_

**Goal:** Bank the trivial deletions that have no Rust coupling and no behavioral surface, to shrink the clone and build momentum before the hard re-points.


**Deletes:** `python/fastapi_turbo/_ws_pipe_bridge.py`, `python/fastapi_turbo/encoders.py`, `python/fastapi_turbo/status.py`, `python/fastapi_turbo/datastructures.py`


**Changes:**

- Delete python/fastapi_turbo/_ws_pipe_bridge.py (61 LOC) — verified only importer is __init__.py; the WS path uses crossbeam channels in websocket.rs, not the pipe. Remove its import line from python/fastapi_turbo/__init__.py.
- encoders.py (259 LOC): real fastapi.encoders.jsonable_encoder has an AST-identical kwonly signature. Repoint compat/fastapi_shim.py's fastapi.encoders entry to real fastapi.encoders; repoint python/fastapi_turbo/__init__.py:7 to import jsonable_encoder from fastapi.encoders; delete encoders.py. NOTE: _json_default lives in responses.py NOT encoders.py, so encoders is fully isolated.
- status.py (107 LOC): pure constant table. Repoint compat/starlette_shim.py + fastapi_shim.py status entries and __init__.py:44 to starlette.status; delete status.py.
- datastructures.py (610 LOC): repoint URL/Headers/MutableHeaders/QueryParams/FormData/Address/URLPath/Secret/State to starlette.datastructures in both compat shims; SOURCE DefaultPlaceholder/Default from fastapi.datastructures (FastAPI-internal). The door never constructs these (it builds plain PyDict scopes), so zero Rust risk. Delete datastructures.py once importers (requests/responses/security/_introspect/etc.) are repointed to starlette.datastructures.


**Gate:** `FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q  AND  python -m pytest tests/parity/test_parity.py -q`


### Phase 1 — params/security/dependencies drop-ins + SecurityScopes door re-point  _(risk=medium, effort=medium)_

**Goal:** Delete the parameter-marker and dependency re-export layers and re-point the door's SecurityScopes construction to real fastapi, since SecurityScopes is field-identical.


**Deletes:** `python/fastapi_turbo/dependencies.py`, `python/fastapi_turbo/security.py`, `python/fastapi_turbo/param_functions.py (markers only; UploadFile relocated, not deleted, until Phase 4)`


**Changes:**

- dependencies.py (24 LOC) is ALREADY a pure re-export of real fastapi.params.Depends/Security. Repoint __init__.py:6 + every importer to `from fastapi.params import Depends, Security`; delete dependencies.py.
- param_functions.py (292 LOC): markers already subclass real fastapi.params.*. Drop _introspect.py/_openapi.py reliance on the clone-only `_kind` attr (the _MARKER_KIND_MAP class-name fallback already exists at _introspect.py:287), then re-export Query/Path/Header/Cookie/Body/Form/File from fastapi.params. KEEP UploadFile in this module FOR NOW (it ABC-wraps the Rust PyUploadFile via __subclasshook__; real starlette UploadFile wraps SpooledTemporaryFile and will NOT recognize PyUploadFile) — UploadFile leaves in Phase 4 with the type bridge.
- security.py (677 LOC): re-point the door's SecurityScopes construction at router.rs:306-311 from fastapi_turbo.security to fastapi.security (verified field-identical: scopes/scope_str). Fix _openapi.py:2670/2697 to accept a SecurityBase whose .model is a Pydantic model (model.model_dump(by_alias=True, exclude_none=True)) instead of gating on isinstance(.model, dict). Verify the clone runtime resolver passes a real Request to scheme __call__ (the adapter _emit_dep path already does this). Then re-export OAuth2*/HTTP*/APIKey*/OpenIdConnect/SecurityScopes/OAuth2PasswordRequestForm* from fastapi.security; delete security.py.
- Update _resolution.py security-base isinstance checks (lines ~172-188) to import the real security base classes in lockstep.


**Gate:** `python -m pytest tests/parity/test_parity.py tests/stress/test_asgi_in_process_security_scopes.py tests/test_pivot_adapter.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q`


### Phase 2 — Relocate door-runtime glue out of _introspect (unblock both _introspect deletion AND adapter circularity)  _(risk=medium, effort=low)_

**Goal:** Move the 4 turbo-internal door helpers that router.rs hard-imports + the symbol the adapter imports from _introspect into a small KEPT door-support module, so _introspect.py becomes deletable later and the adapter stops depending on it.


**Changes:**

- Create python/fastapi_turbo/_door_support.py. Move _make_fa_body_validator/_FABodyValidator/_TypeAdapterProxy/_PARAM_MODEL_MISSING and _get_type_name from _introspect.py into it.
- Re-point src/router.rs:1190 from `py.import("fastapi_turbo._introspect")` to `py.import("fastapi_turbo._door_support")` for getattr("_make_fa_body_validator"). This is the SINGLE Rust change in this phase — rebuild with maturin develop.
- Re-point _introspect_from_real_fastapi.py:51 (imports _get_type_name from _introspect) and _openapi.py:1507 (imports _PARAM_MODEL_MISSING) to _door_support. This breaks the adapter↔_introspect circular dependency.
- Keep _set_current_request_scope (applications.py, called by router.rs:94) and _async_worker/_async_bridge where they are (they are kept-forever door glue, not FA/Starlette concepts).


**Gate:** `PATH="$HOME/.cargo/bin:$PATH" maturin develop  THEN  python -m pytest tests/parity/test_parity.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q`


### Phase 3 — Peripheral pure-clones to real (sse/authentication/staticfiles/exceptions)  _(risk=medium, effort=medium)_

**Goal:** Replace the no-Rust-coupling peripheral FastAPI/Starlette surface clones with re-exports of the real packages.


**Deletes:** `python/fastapi_turbo/sse.py`, `python/fastapi_turbo/authentication.py`, `python/fastapi_turbo/staticfiles.py`, `python/fastapi_turbo/exceptions.py (or shrunk to a small errors()-coercion wrapper)`


**Changes:**

- sse.py (98 LOC): repoint to real fastapi.sse (ServerSentEvent/format_sse_event/KEEPALIVE_COMMENT near-identical). FIRST fix responses.py to import ServerSentEvent from real fastapi.sse (breaking the responses<->sse circular re-export), and confirm responses.EventSourceResponse byte-matches real fastapi.sse.EventSourceResponse format. Repoint shims + __init__.py:23; delete sse.py.
- authentication.py (247 LOC): repoint to starlette.authentication (BaseUser/SimpleUser/AuthCredentials/AuthenticationBackend/requires/AuthenticationMiddleware all exist). VERIFY the _fastapi_turbo_middleware_type='python_http_auth' marker that applications.py/_middleware_wrap.py keys on is preserved (wrap real AuthenticationMiddleware to carry it, or teach the config builder to recognize the real class). Repoint shims; delete authentication.py.
- staticfiles.py (158 LOC): repoint to starlette.staticfiles.StaticFiles (the clone async __call__ is in-process-only; the Rust app.run() path uses Tower ServeDir, which reads mounted_app.directory — verify the real StaticFiles exposes .directory). Repoint shims; delete staticfiles.py.
- exceptions.py (159 LOC): repoint to starlette.exceptions + fastapi.exceptions. BLOCKER FIRST: the Rust validation-error extractor emits loc as a LIST; real pydantic/fastapi emit loc as a TUPLE; clone ValidationException.errors() coerces list->tuple (exceptions.py:42-48). Either change Rust to emit tuples OR keep a 10-line errors()-coercing subclass. Also verify real RequestValidationError(subclass of pydantic.ValidationError) constructor matches how Rust raises it. Then delete (or shrink to the coercion shim).


**Gate:** `python -m pytest tests/parity/test_parity.py -q  AND  python -m pytest tests/test_websocket.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q`


### Phase 4 — Type bridge: Response (bytes raw_headers) + UploadFile + WebSocket -> 100% adapter default-on  _(risk=high, effort=high)_

**Goal:** Close the THREE remaining un-bridged framework types in _clone_framework_types() so the pivot adapter stops declining and can be flipped default-ON. The Response work simultaneously fixes the responses.rs re-point gate.


**Changes:**

- RESPONSE (the critical one): rewrite the Rust header readers to consume real starlette's list[(bytes,bytes)] raw_headers + MutableHeaders. Edit responses.rs:353/365 (response_object_to_response raw_header_keys + raw_headers append) and responses.rs:990 (extract_response_headers) and router.rs:453/489 (apply_injected_response raw_keys + append) to extract::<(Vec<u8>,Vec<u8>)> (or PyBytes) and build HeaderName/HeaderValue from bytes; keep the MutableHeaders .items() fallback for the headers dict path. THEN make clone Response subclass starlette.responses.Response (or switch responses.py imports to starlette and keep only turbo-specific subclasses) so _clone_framework_types() drops Response. THEN re-point responses.rs init_response_classes (responses.rs:34-49) + JSON_DEFAULT + router.rs response_cls (router.rs:68) to starlette.responses ATOMICALLY (else identity fast-path stops matching and every response falls to the slow getattr probe).
- UPLOADFILE: keep the clone UploadFile ABC (it virtual-subclasses the Rust PyUploadFile via __subclasshook__) but register real starlette.datastructures.UploadFile and PyUploadFile as virtual subclasses of it, OR teach the adapter to accept the real UploadFile annotation while the door keeps returning PyUploadFile. Goal: remove UploadFile from _clone_framework_types() so form/file routes stop declining.
- WEBSOCKET: keep the clone WebSocket wrapper (websocket.rs:738 constructs WebSocket(ws_cell); real starlette ctor is (scope,receive,send) — incompatible) but bridge the TYPE so route signatures using WebSocket are introspectable by real fastapi; add WS support to the adapter or keep WS on the clone introspect path (router.dependant) while still bridging the type.
- Remove the three decline guards in _adapter_route_info (_signature_uses_clone_framework_type, _signature_uses_form_file_marker, the response_class!=JSONResponse check now that Response is bridged), close _combined_dependencies/status_code/dependency_overrides declines, then flip the adapter DEFAULT-ON (drop the FASTAPI_TURBO_ADAPTER!=1 early return at applications.py:7201).


**Gate:** `PATH="$HOME/.cargo/bin:$PATH" maturin develop  THEN  python -m pytest tests/parity/test_parity.py -q (MUST stay 151 — this is the byte-diff cookie/header/charset gate)  AND  python -m pytest tests/test_pivot_adapter.py tests/test_websocket.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q`


### Phase 5 — Re-point compat shim to thin pass-through + retarget door's Request/BackgroundTasks constructors  _(risk=high, effort=high)_

**Goal:** Flip the master gate: stop overwriting sys.modules for fastapi/starlette; leave real packages live and re-point ONLY fastapi.FastAPI -> accelerated. Re-point the door's last clone constructors so requests.py/background.py can die.


**Deletes:** `python/fastapi_turbo/compat/fastapi_shim.py`, `python/fastapi_turbo/compat/starlette_shim.py`, `python/fastapi_turbo/_starlette_compat.py`, `python/fastapi_turbo/_compat_shim.py`, `python/fastapi_turbo/requests.py`, `python/fastapi_turbo/background.py (if door uses real API; else shrunk to a subclass)`


**Changes:**

- Rewrite compat/__init__.py::install() to STOP doing sys.modules.update(MODULES) and instead leave real fastapi/starlette in sys.modules, then `import fastapi; fastapi.FastAPI = fastapi_turbo.applications.FastAPI; fastapi.applications.FastAPI = ...` (preserve the verified mapping: `from fastapi import FastAPI` resolves to the accelerated subclass, __version__ 0.136.0). This makes Depends/Query/Request/Response/etc. fall through to REAL fastapi/starlette. Delete fastapi_shim.py (1072 LOC), starlette_shim.py (473 LOC), _starlette_compat.py (624 LOC — Route/Mount/Host/HTTPEndpoint/etc. now come from real starlette for free), _compat_shim.py (172 LOC — fastapi._compat now real).
- REQUESTS: change router.rs build_injected_object (inject_request, ~router.rs:260-287) to supply a real ASGI receive() returning the buffered body, instead of pre-stashing scope['_body']. Then re-point router.rs request_cls (router.rs:56) to starlette.requests.Request. Then delete requests.py (588 LOC) — already subclasses real starlette, only body()-via-scope['_body'] kept it.
- BACKGROUNDTASKS: change the door to use the real BackgroundTasks API. Either (A) make router.rs drain_background_tasks (router.rs:388-412) read .tasks + drive async __call__ via the worker loop instead of ._tasks/.run_sync, and router.rs:297 stop setting ._app; OR (B) keep a 15-line BackgroundTasks(starlette) subclass adding run_sync/_app. Prefer (A) to fully delete background.py (80 LOC). Re-point bg_tasks_cls (router.rs:44).
- Update the ~12 parity runner scripts + test_compat.py that call compat.install()/assert shim round-trip to the new thin-shim semantics.


**Gate:** `PATH="$HOME/.cargo/bin:$PATH" maturin develop  THEN  python -m pytest tests/parity/test_parity.py -q (byte-diff: catches body/header divergence)  AND  python -m pytest tests/test_compat.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q`


### Phase 6 — Pivot routing + OpenAPI to real fastapi (delete clone routing/openapi/introspect/resolution)  _(risk=high, effort=high)_

**Goal:** Make app.router a real fastapi.routing.APIRouter, walk real route trees in the collector, and switch OpenAPI to real fastapi.openapi.utils.get_openapi. This unblocks deleting the clone introspection brain.


**Deletes:** `python/fastapi_turbo/routing.py`, `python/fastapi_turbo/_openapi.py`, `python/fastapi_turbo/_introspect.py`, `python/fastapi_turbo/_resolution.py`, `python/fastapi_turbo/_route_helpers.py (clone-only parts)`


**Changes:**

- ROUTING: rebase _has_overridden_get_route_handler (_route_helpers.py:431) against real fastapi.routing.APIRoute.get_route_handler FIRST (cheap, prevents misfire on every real route). Make __init__ stop overwriting self.router with a clone APIRouter (applications.py:3224) — use the real APIRouter super() already built. Rewrite _collect_routes_from_router/_collect_all_routes (applications.py:5102/5934) to walk real self.routes (real include_router eager-flattens, so drop the _included_routers/_is_included_shadow machinery). Switch the WS gate from getattr(route,'_is_websocket') to isinstance(route, APIWebSocketRoute). Construct dynamic docs/openapi/redoc routes as real APIRoute re-attaching _fastapi_turbo_dynamic_route/_fastapi_turbo_bypass_deps markers. Keep handler._fastapi_turbo_route_obj wiring (door only needs .path + scope['route']). Delete the 8 _assert_* validators + clone __init__ + clone APIRoute/APIRouter classes; keep the WS-adapt module-level helpers. Delete routing.py (1313 LOC).
- OPENAPI: persist the real fastapi APIRoute that _adapter_route_info builds (applications.py:7274 — currently discarded after ParamInfo extraction) so they back real get_openapi(routes=...). Replace generate_openapi_schema call in openapi() (applications.py:6259) with a thin wrapper over fastapi.openapi.utils.get_openapi (map openapi_tags->tags, drop openapi_url, pass real BaseRoute sequences). Door needs zero change (still gets an opaque openapi_json string). Diff-pass the schema bytes against the suite. Delete _openapi.py (2909 LOC).
- INTROSPECTION/RESOLUTION: with the adapter default-on (Phase 4) and OpenAPI off the clone, nothing reaches introspect_endpoint/build_resolution_plan for HTTP. Switch WS dispatch off introspect_endpoint to route.dependant. Delete _introspect.py (2026 LOC, minus the Phase-2-relocated helpers), _resolution.py (736 LOC), and the clone-only parts of _route_helpers.py (801 LOC; _apply_response_model superseded by adapter _serialize_via_field).


**Gate:** `python -m pytest tests/parity/test_parity.py -q  AND  python -m pytest tests/stress/test_route_class_*.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q (heavy app.openapi() + include_router/url_path_for assertion surface)`


### Phase 7 — Delete the Python in-process ASGI dispatcher (the SECOND request engine) + drop API-surface overrides  _(risk=high, effort=high)_

**Goal:** Remove the ~5.1K-LOC duplicate request engine inside applications.py and drop the route/middleware/openapi/url_path_for overrides to super(), leaving only the irreducible Rust-door engine glue.


**Deletes:** `python/fastapi_turbo/middleware/cors.py`, `python/fastapi_turbo/middleware/gzip.py`, `python/fastapi_turbo/middleware/httpsredirect.py`, `python/fastapi_turbo/middleware/base.py`, `applications.py group-B dispatcher (~5.1K LOC, in-file deletion) + group-C overrides (~1.5K LOC, in-file)`


**Changes:**

- PREREQUISITE GATE: add a cross-engine diff test (or retarget TestClient.__call__ at the Rust door) — per project_dual_request_engine memory there is NO cross-engine byte-diff test today; the ~1122 suite runs the Python dispatcher via httpx.ASGITransport while only tests/parity exercises Rust. Confirm the full suite stays green against the Rust door BEFORE deleting the dispatcher.
- Replace __call__'s http/websocket branches (applications.py:8093) with: oneshot-door-if-enabled, else delegate to the existing loopback Rust proxy (_asgi_proxy_http/_asgi_proxy_websocket, already present) OR super().__call__. Delete _asgi_dispatch_in_process (~3334 LOC, applications.py:8372), _asgi_dispatch_ws_in_process (~901 LOC, applications.py:11705), _wrap_websocket_endpoint (~894 LOC, applications.py:4150), and the dispatch branches.
- Drop group-C API-surface overrides to super(): get/post/include_router/mount/routes/openapi/url_path_for/exception_handler/on_event/add_middleware (applications.py:3316-3970) now that collection reads real self.routes.
- Retarget/delete tests that assert on _asgi_dispatch_* / oneshot internals (tests/test_oneshot_inprocess_door.py, test_p0/p1_parity.py, stress/test_r33/r37/r40).
- Middleware: repoint _build_middleware_config + add_middleware to recognize REAL starlette.middleware classes (CORS/GZip/TrustedHost/HTTPSRedirect) by identity and extract their kwargs into the Rust `type` dict; delete the inert markers middleware/{cors,gzip,httpsredirect,base}.py + the markers in trustedhost/sessions (replace sessions/trustedhost with real Starlette via the raw-ASGI shim). KEEP _middleware_wrap.py as the door's kwargs->ASGI bridge (rename to clarify it is door infra, not a clone) — the Rust door cannot host Starlette's ASGI middleware stack.


**Gate:** `python -m pytest tests/parity/test_parity.py -q  AND  python -m pytest tests/test_websocket.py -q  AND  FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT=1 python -m pytest tests/ --ignore=tests/test_websocket.py -q (the FULL suite must now ride the Rust door, not the deleted Python dispatcher)`


## Open questions

- Cross-engine blind spot: project_dual_request_engine memory says there is NO byte-diff test between the Python dispatcher (what the 1122 suite runs via httpx.ASGITransport) and the Rust door (only tests/parity runs). Phase 7 needs either a cross-engine diff gate added FIRST or TestClient retargeted at the Rust door — which is it, and does retargeting TestClient slow the suite unacceptably (real HTTP per test)?
- loc-as-list-vs-tuple (exceptions.py): is it cleaner to change the Rust validation extractor to emit loc as a tuple, or keep a 10-line errors()-coercion subclass forever? Affects whether exceptions.py deletes fully in Phase 3.
- Sentry parity: _middleware_wrap.py + _sentry_compat.py + trustedhost.py's __module__-masquerade exist for Sentry transaction naming. Is Sentry parity a hard requirement? If droppable, Phase 7 deletes ~400 more LOC of middleware glue.
- UploadFile bridge strategy (Phase 4): register real starlette UploadFile + Rust PyUploadFile as virtual subclasses of the clone ABC, OR make the adapter accept the real annotation while the door returns PyUploadFile? The former keeps form/file isinstance checks working without door changes.
- Does real starlette StaticFiles expose a .directory attribute that applications.py static-mount collection (and the Rust Tower ServeDir path) reads? If not, Phase 3 staticfiles deletion needs a thin wrapper.
- After Phase 6 makes app.router real, do custom route_class tests (tests/stress/test_route_class_*.py) that subclass the CLONE APIRoute need migration to subclass real fastapi.routing.APIRoute, or a compat alias?
- WebSocket adapter coverage (Phase 4): is it acceptable to keep WS routes on the clone introspect path (route.dependant) indefinitely while only bridging the WS TYPE, or must the adapter gain a full WS path before _introspect.py can be deleted in Phase 6?


## Status
- Phase 0: STARTED — deleted `_ws_pipe_bridge.py`, `encoders.py`→real (commit debf76b). Remaining Phase 0: `status.py`, `datastructures.py` need dependent re-pointing (not clean drops).

## Execution log
- **Phase 0 (started, commit debf76b):** deleted `_ws_pipe_bridge.py` (dead); `encoders.py` → re-export real `fastapi.encoders`. parity 151 / full 1122 / WS 22 green. Remaining Phase 0 items (`status.py`, `datastructures.py`) are NOT clean drops — they have live dependents in modules that stay (requests.py, websockets.py, applications.py), so they need dependent re-pointing (fold into Phase 5/6).
- **Phase 2 finding:** the door-glue symbols to relocate (`_get_type_name`, `_make_fa_body_validator`, `_FABodyValidator`, `_TypeAdapterProxy`) are NOT self-contained — they transitively call ~14 other `_introspect` helpers (`_is_union_origin`, `_get_container_type`, `_build_field_info`, `_make_type_adapter_proxy`, `_maybe_embed_body_params`, `_needs_scalar_validator`, `_special_injection_kind`, `_unwrap_optional`, …). Phase 2 must move the door-needed **dependency closure** into `_door_support.py` (or split _introspect into door-support vs clone-only), then re-point src/router.rs:1191 + `_introspect_from_real_fastapi.py:51` + `_openapi.py:1507`. Needs a dedicated pass.
- **Phase 2 (DONE, commit 9f5d0af):** AST-computed the door-glue closure (clean: 5 symbols — `_is_union_origin`, `_get_type_name`, `_TypeAdapterProxy`, `_FABodyValidator`, `_make_fa_body_validator`) and moved them verbatim to new KEPT module `_door_support.py`. `_introspect.py` re-imports them (2026→1829 LOC). Re-pointed `src/router.rs` + `_introspect_from_real_fastapi.py` to `_door_support`. Verified: `src/` imports NO `_introspect`; adapter imports NO `_introspect` (circular broken). parity 151 (OFF+ON), full 1122, WS 22.
