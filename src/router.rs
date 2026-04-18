use axum::body::Body;
use axum::extract::{Path, Query, Request};
use axum::extract::ws::WebSocketUpgrade;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{MethodRouter, any, get, post, put, delete, patch, head};
use axum::Router;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::Arc;

use crate::handler_bridge::call_async_handler;
use crate::multipart::{parse_boundary, parse_multipart, ParsedField, PyUploadFile};
use crate::responses::{py_to_response, pyerr_to_response, serde_to_pyobj};
use crate::websocket::handle_ws_upgrade;

static BG_TASKS_CLS: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
static REQUEST_CLS: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
static RESPONSE_CLS: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
/// The FastAPI application instance — set at `app.run()` so injected
/// Request objects can expose `request.app`. vLLM/SGLang read
/// `request.app.state.*` from every handler, so this is required.
pub static APP_INSTANCE: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
/// Python callable invoked when Rust-side parameter/body validation fails.
/// Called only when the app registers `@exception_handler(RequestValidationError)`
/// — otherwise we use the default 422 body path.
pub static VALIDATION_HANDLER: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();

fn bg_tasks_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = BG_TASKS_CLS.get() { return Ok(c); }
    let cls: Py<PyAny> = py
        .import("fastapi_rs.background")?
        .getattr("BackgroundTasks")?
        .unbind();
    let _ = BG_TASKS_CLS.set(cls);
    Ok(BG_TASKS_CLS.get().unwrap())
}

fn request_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = REQUEST_CLS.get() { return Ok(c); }
    let cls: Py<PyAny> = py
        .import("fastapi_rs.requests")?
        .getattr("Request")?
        .unbind();
    let _ = REQUEST_CLS.set(cls);
    Ok(REQUEST_CLS.get().unwrap())
}

fn response_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = RESPONSE_CLS.get() { return Ok(c); }
    let cls: Py<PyAny> = py
        .import("fastapi_rs.responses")?
        .getattr("Response")?
        .unbind();
    let _ = RESPONSE_CLS.set(cls);
    Ok(RESPONSE_CLS.get().unwrap())
}

/// Inject request metadata (method, path, query, headers) into kwargs for
/// the middleware wrapper. Called for every handler invocation so that
/// `BaseHTTPMiddleware.dispatch()` can inspect `request.url.path`,
/// `request.headers`, etc. Cost: ~2-3μs (3 string copies + header list).
fn inject_request_metadata(
    py: Python<'_>,
    kwargs: &Bound<'_, PyDict>,
    scope_method: &Option<String>,
    scope_path: &Option<String>,
    scope_query: &Option<String>,
    headers: &Option<HeaderMap>,
) {
    if let Some(ref m) = scope_method {
        let _ = kwargs.set_item("_request_method", m);
    }
    if let Some(ref p) = scope_path {
        let _ = kwargs.set_item("_request_path", p);
    }
    if let Some(ref q) = scope_query {
        let _ = kwargs.set_item("_request_query", q);
    }
    if let Some(ref h) = headers {
        let hdrs = pyo3::types::PyList::empty(py);
        for (k, v) in h.iter() {
            let _ = hdrs.append((
                pyo3::types::PyBytes::new(py, k.as_str().as_bytes()),
                pyo3::types::PyBytes::new(py, v.as_bytes()),
            ));
        }
        let _ = kwargs.set_item("_request_headers", hdrs);
    }
}

/// Inject framework-provided objects (Request / BackgroundTasks / Response)
/// as handler kwargs right before dispatch. Handlers ask for them by type.
fn inject_framework_objects(
    py: Python<'_>,
    kwargs: &Bound<'_, PyDict>,
    state: &RouteState,
    scope_method: &Option<String>,
    scope_path: &Option<String>,
    scope_query: &Option<String>,
    headers: &Option<HeaderMap>,
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    body_bytes: &[u8],
) -> PyResult<()> {
    for param in &state.params {
        match param.kind.as_str() {
            "inject_request" => {
                // Build an ASGI-ish scope dict
                let scope = PyDict::new(py);
                scope.set_item("type", "http")?;
                scope.set_item("method", scope_method.as_deref().unwrap_or("GET"))?;
                scope.set_item("path", scope_path.as_deref().unwrap_or("/"))?;
                let qs_bytes: &[u8] =
                    scope_query.as_deref().map(|s| s.as_bytes()).unwrap_or(b"");
                scope.set_item(
                    "query_string",
                    pyo3::types::PyBytes::new(py, qs_bytes),
                )?;
                // Headers as list of (bytes, bytes)
                let hdrs_list = pyo3::types::PyList::empty(py);
                if let Some(h) = headers {
                    for (k, v) in h.iter() {
                        let k_b = pyo3::types::PyBytes::new(py, k.as_str().as_bytes());
                        let v_b = pyo3::types::PyBytes::new(py, v.as_bytes());
                        hdrs_list.append((k_b, v_b))?;
                    }
                }
                scope.set_item("headers", hdrs_list)?;
                // Path params
                let pp = PyDict::new(py);
                for (k, v) in path_map.iter() {
                    pp.set_item(k, v)?;
                }
                scope.set_item("path_params", pp)?;
                // Query params as a dict too (convenience)
                let qp = PyDict::new(py);
                for (k, v) in query_params.iter() {
                    qp.set_item(k, v)?;
                }
                scope.set_item("query_params", qp)?;
                // Starlette/FastAPI: request.app -> scope["app"]. vLLM and
                // SGLang read `request.app.state.<field>` on every request.
                if let Some(app) = APP_INSTANCE.get() {
                    scope.set_item("app", app.bind(py))?;
                }
                // Pre-populate the body so `await request.body()` / .json()
                // / .form() return the already-buffered bytes without needing
                // a real ASGI receive() callable. vLLM parses bodies this way.
                if !body_bytes.is_empty() {
                    scope.set_item(
                        "_body",
                        pyo3::types::PyBytes::new(py, body_bytes),
                    )?;
                }

                let req = request_cls(py)?.bind(py).call1((scope,))?;
                kwargs.set_item(&param.name, req)?;
            }
            "inject_background_tasks" => {
                let bg = bg_tasks_cls(py)?.bind(py).call0()?;
                kwargs.set_item(&param.name, bg)?;
            }
            "inject_response" => {
                let resp = response_cls(py)?.bind(py).call0()?;
                kwargs.set_item(&param.name, resp)?;
            }
            "inject_security_scopes" => {
                // Empty SecurityScopes — real scope collection from
                // nested Security() dep chain happens in the resolver.
                let ss_mod = py.import("fastapi_rs.security")?;
                let ss_cls = ss_mod.getattr("SecurityScopes")?;
                let scopes_list = pyo3::types::PyList::empty(py);
                let kw = PyDict::new(py);
                kw.set_item("scopes", scopes_list)?;
                let obj = ss_cls.call((), Some(&kw))?;
                kwargs.set_item(&param.name, obj)?;
            }
            _ => {}
        }
    }
    Ok(())
}

/// After the handler returns, any BackgroundTasks instance the handler
/// received gets DEFERRED — tasks run on a tokio blocking thread after
/// the response is flushed, matching FastAPI/Starlette semantics.
/// The handler doesn't wait for task completion.
fn drain_background_tasks(
    py: Python<'_>,
    kwargs: &Bound<'_, PyDict>,
    params: &[ParamInfo],
) {
    for param in params {
        if param.kind == "inject_background_tasks" {
            if let Ok(Some(bg_obj)) = kwargs.get_item(&param.name) {
                // Extract the BackgroundTasks instance as an unbound Py<PyAny>
                // so we can ship it to a detached blocking thread.
                let owned: Py<PyAny> = bg_obj.clone().unbind();
                // Only spawn if there's work queued — inspect via _tasks attr.
                let has_tasks = owned
                    .bind(py)
                    .getattr("_tasks")
                    .ok()
                    .and_then(|t| t.len().ok())
                    .map(|n| n > 0)
                    .unwrap_or(false);
                if !has_tasks {
                    continue;
                }
                tokio::task::spawn_blocking(move || {
                    Python::attach(|py| {
                        let _ = owned.bind(py).call_method0("run_sync");
                    });
                });
            }
        }
    }
}

/// If the handler was given an injected Response and mutated it, carry
/// those headers / status_code forward onto the actual response. This
/// is how FastAPI lets handlers do:
///
///     def h(response: Response):
///         response.status_code = 201
///         response.headers["x-custom"] = "1"
///         return {"ok": True}
fn apply_injected_response(
    py: Python<'_>,
    kwargs: &Bound<'_, PyDict>,
    params: &[ParamInfo],
    response: &mut Response,
) {
    for param in params {
        if param.kind != "inject_response" {
            continue;
        }
        let Ok(Some(obj)) = kwargs.get_item(&param.name) else { continue };
        // Apply status_code (but only if user set something non-default)
        if let Ok(sc_attr) = obj.getattr("status_code") {
            if let Ok(sc) = sc_attr.extract::<u16>() {
                if sc != 200 {
                    if let Ok(s) = StatusCode::from_u16(sc) {
                        *response.status_mut() = s;
                    }
                }
            }
        }
        // Merge headers dict (iterate .headers)
        if let Ok(hdr) = obj.getattr("headers") {
            if let Ok(dict) = hdr.cast::<PyDict>() {
                let _ = py;
                for (k, v) in dict.iter() {
                    if let (Ok(ks), Ok(vs)) = (k.extract::<String>(), v.extract::<String>()) {
                        if let (Ok(hn), Ok(hv)) =
                            (HeaderName::try_from(ks), HeaderValue::from_str(&vs))
                        {
                            response.headers_mut().insert(hn, hv);
                        }
                    }
                }
            }
        }
    }
}

/// Run a per-param Pydantic TypeAdapter against the coerced value. If
/// validation fails, return a 422 with a FastAPI-compatible error body
/// built from Pydantic's own errors.
fn run_scalar_validator<'py>(
    py: Python<'py>,
    param: &ParamInfo,
    loc: &str,
    value: &Bound<'py, PyAny>,
) -> Result<Bound<'py, PyAny>, Response> {
    let Some(ref adapter) = param.scalar_validator else {
        return Ok(value.clone());
    };
    match adapter.call_method1(py, "validate_python", (value,)) {
        Ok(v) => Ok(v.into_bound(py)),
        Err(e) => Err(pydantic_error_response(py, &e, loc)),
    }
}

/// Apply a parameter's default to the kwargs dict. Honors `has_default`:
/// when the marker declares `default=None`, we pass Python `None` explicitly
/// so the handler doesn't fall back to the function signature's default
/// (which would be the marker object itself).
fn apply_default<'py>(
    py: Python<'py>,
    kwargs: &Bound<'py, PyDict>,
    param: &ParamInfo,
) -> bool {
    if !param.has_default {
        return false;
    }
    match &param.default_value {
        Some(v) => {
            let _ = kwargs.set_item(&param.name, v.bind(py));
        }
        None => {
            let _ = kwargs.set_item(&param.name, py.None());
        }
    }
    true
}

/// Construct a `PyUploadFile` directly — no Python `UploadFile` wrapper needed
/// because `PyUploadFile` now implements the full async Starlette interface
/// (read/seek/close return `ImmediateBytes` / `ImmediateNone` awaitables).
/// `isinstance(f, UploadFile)` still works via the ABCMeta subclasshook on
/// the Python `UploadFile` class.
fn make_upload_file<'py>(
    py: Python<'py>,
    field: ParsedField,
) -> PyResult<Bound<'py, PyAny>> {
    let up = PyUploadFile::from_field(field);
    let py_up = Py::new(py, up)?;
    Ok(py_up.into_bound(py).into_any())
}

// ── Data types exposed to Python ──────────────────────────────────────

#[pyclass(from_py_object)]
#[derive(Debug)]
pub struct ParamInfo {
    #[pyo3(get, set)]
    pub name: String,
    #[pyo3(get, set)]
    pub kind: String,
    #[pyo3(get, set)]
    pub type_hint: String,
    #[pyo3(get, set)]
    pub required: bool,
    #[pyo3(get, set)]
    pub default_value: Option<Py<PyAny>>,
    /// True when the parameter has a default declared (even if the default
    /// value is Python `None`). Lets us distinguish "no default" (no kwarg
    /// passed — Python uses the function's signature default) from
    /// "default is None" (pass `None` explicitly).
    #[pyo3(get, set)]
    pub has_default: bool,
    #[pyo3(get, set)]
    pub model_class: Option<Py<PyAny>>,
    /// Cached SchemaValidator — avoids getattr("__pydantic_validator__") per-request
    pub cached_validator: Option<Py<PyAny>>,
    /// Scalar Pydantic TypeAdapter for constrained query/path/header/cookie
    /// params (e.g. ``Query(ge=1, le=100)``). If set, we call
    /// ``validate_python(value)`` on the coerced Python value to surface
    /// FastAPI-equivalent ``ge``/``le``/``min_length`` etc 422 errors.
    #[pyo3(get, set)]
    pub scalar_validator: Option<Py<PyAny>>,
    #[pyo3(get, set)]
    pub alias: Option<String>,
    #[pyo3(get, set)]
    pub dep_callable: Option<Py<PyAny>>,
    #[pyo3(get, set)]
    pub dep_callable_id: Option<u64>,
    #[pyo3(get, set)]
    pub is_async_dep: bool,
    #[pyo3(get, set)]
    pub is_generator_dep: bool,
    #[pyo3(get, set)]
    pub dep_input_names: Vec<(String, String)>,
    #[pyo3(get, set)]
    pub is_handler_param: bool,
}

impl Clone for ParamInfo {
    fn clone(&self) -> Self {
        Python::attach(|py| ParamInfo {
            name: self.name.clone(),
            kind: self.kind.clone(),
            has_default: self.has_default,
            type_hint: self.type_hint.clone(),
            required: self.required,
            default_value: self.default_value.as_ref().map(|v| v.clone_ref(py)),
            model_class: self.model_class.as_ref().map(|v| v.clone_ref(py)),
            cached_validator: self.cached_validator.as_ref().map(|v| v.clone_ref(py)),
            scalar_validator: self.scalar_validator.as_ref().map(|v| v.clone_ref(py)),
            alias: self.alias.clone(),
            dep_callable: self.dep_callable.as_ref().map(|v| v.clone_ref(py)),
            dep_callable_id: self.dep_callable_id,
            is_async_dep: self.is_async_dep,
            is_generator_dep: self.is_generator_dep,
            dep_input_names: self.dep_input_names.clone(),
            is_handler_param: self.is_handler_param,
        })
    }
}

#[pymethods]
impl ParamInfo {
    #[new]
    #[pyo3(signature = (name, kind, type_hint="str".to_string(), required=true, default_value=None, has_default=false, model_class=None, alias=None, dep_callable=None, dep_callable_id=None, is_async_dep=false, is_generator_dep=false, dep_input_names=vec![], is_handler_param=true, scalar_validator=None))]
    fn new(
        name: String,
        kind: String,
        type_hint: String,
        required: bool,
        default_value: Option<Py<PyAny>>,
        has_default: bool,
        model_class: Option<Py<PyAny>>,
        alias: Option<String>,
        dep_callable: Option<Py<PyAny>>,
        dep_callable_id: Option<u64>,
        is_async_dep: bool,
        is_generator_dep: bool,
        dep_input_names: Vec<(String, String)>,
        is_handler_param: bool,
        scalar_validator: Option<Py<PyAny>>,
    ) -> Self {
        ParamInfo {
            name, kind, type_hint, required, default_value, has_default, model_class,
            scalar_validator,
            cached_validator: None, // Populated at startup by build_router
            alias, dep_callable, dep_callable_id, is_async_dep, is_generator_dep,
            dep_input_names, is_handler_param,
        }
    }
}

#[pyclass(from_py_object)]
#[derive(Debug)]
pub struct RouteInfo {
    #[pyo3(get, set)]
    pub path: String,
    #[pyo3(get, set)]
    pub methods: Vec<String>,
    #[pyo3(get, set)]
    pub handler: Py<PyAny>,
    #[pyo3(get, set)]
    pub is_async: bool,
    #[pyo3(get, set)]
    pub handler_name: String,
    #[pyo3(get, set)]
    pub params: Vec<ParamInfo>,
    #[pyo3(get, set)]
    pub is_websocket: bool,
}

impl Clone for RouteInfo {
    fn clone(&self) -> Self {
        Python::attach(|py| RouteInfo {
            path: self.path.clone(),
            methods: self.methods.clone(),
            handler: self.handler.clone_ref(py),
            is_async: self.is_async,
            handler_name: self.handler_name.clone(),
            params: self.params.clone(),
            is_websocket: self.is_websocket,
        })
    }
}

#[pymethods]
impl RouteInfo {
    #[new]
    #[pyo3(signature = (path, methods, handler, is_async=false, handler_name="".to_string(), params=vec![], is_websocket=false))]
    fn new(
        path: String, methods: Vec<String>, handler: Py<PyAny>,
        is_async: bool, handler_name: String, params: Vec<ParamInfo>,
        is_websocket: bool,
    ) -> Self {
        RouteInfo { path, methods, handler, is_async, handler_name, params, is_websocket }
    }
}

// ── Path conversion ───────────────────────────────────────────────────

fn convert_path(fastapi_path: &str) -> String {
    let mut result = String::with_capacity(fastapi_path.len());
    let mut chars = fastapi_path.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '{' {
            let mut param = String::new();
            for c in chars.by_ref() {
                if c == '}' { break; }
                param.push(c);
            }
            if let Some(name) = param.strip_suffix(":path") {
                result.push_str(&format!("{{*{name}}}"));
            } else {
                result.push('{');
                result.push_str(&param);
                result.push('}');
            }
        } else {
            result.push(ch);
        }
    }
    result
}

// ── Compiled route state (built once at startup) ─────────────────────

/// Pre-computed flags to skip unnecessary work on the hot path.
struct RouteState {
    handler: Py<PyAny>,
    params: Vec<ParamInfo>,
    is_async: bool,
    has_body_params: bool,
    has_header_params: bool,
    has_dep_params: bool,
    has_any_params: bool,
    has_inject_request: bool,
    has_inject_background_tasks: bool,
    has_inject_response: bool,
    has_file_params: bool,
    has_form_params: bool,
    has_http_middleware: bool,
    // Note: body validation stays with Pydantic (Rust-backed) for 100% compatibility.
    // jsonschema crate can't handle custom validators, coercion, defaults, etc.
}

struct WsRouteState {
    handler: Py<PyAny>,
    is_async: bool,
}

// ── Router builder ────────────────────────────────────────────────────

/// Build `(http_router, ws_router)`. The HTTP branch is returned *without*
/// a 404 fallback — the caller stitches CORS/middleware around it, then
/// merges the two branches and adds the 404 fallback last. WS routes bypass
/// all middleware because tower-http's CorsLayer mutates the 101 Switching
/// Protocols upgrade response and breaks the handshake.
pub fn build_router(routes: Vec<RouteInfo>) -> (Router, Router) {
    let mut router = Router::new();
    let mut ws_router: Router = Router::new();
    // Accumulate MethodRouter per axum-path so we can merge multiple
    // @app.get/post decorators on the same path, and only then attach the
    // OPTIONS/405 fallbacks (which must be added exactly once per path).
    let mut by_path: Vec<(String, MethodRouter, Vec<String>, bool)> = Vec::new();

    for route in routes {
        let axum_path = convert_path(&route.path);

        if route.is_websocket {
            let ws_state = Arc::new(WsRouteState {
                handler: Python::attach(|py| route.handler.clone_ref(py)),
                is_async: route.is_async,
            });
            ws_router = ws_router.route(
                &axum_path,
                any(
                    move |ws: WebSocketUpgrade,
                          req_parts: axum::http::request::Parts| {
                        let state = ws_state.clone();
                        async move {
                            let h = Python::attach(|py| state.handler.clone_ref(py));
                            let is_a = state.is_async;

                            // Populate scope info from the upgrade request
                            let uri = &req_parts.uri;
                            let path = uri.path().to_string();
                            let raw_path = path.as_bytes().to_vec();
                            let query_string = uri
                                .query()
                                .map(|q| q.as_bytes().to_vec())
                                .unwrap_or_default();
                            let headers: Vec<(String, String)> = req_parts
                                .headers
                                .iter()
                                .map(|(k, v)| {
                                    (k.as_str().to_owned(), v.to_str().unwrap_or("").to_owned())
                                })
                                .collect();
                            let host = req_parts
                                .headers
                                .get("host")
                                .and_then(|h| h.to_str().ok())
                                .unwrap_or("")
                                .to_string();
                            let scheme = if req_parts
                                .headers
                                .get("x-forwarded-proto")
                                .map(|v| v.to_str().unwrap_or("") == "https")
                                .unwrap_or(false)
                            {
                                "wss"
                            } else {
                                "ws"
                            }
                            .to_string();
                            // ConnectInfo requires into_make_service_with_connect_info —
                            // fall back to None for now. Users can read X-Forwarded-For from headers.
                            let client: Option<(String, u16)> = None;

                            let scope = crate::websocket::WsScopeInfo {
                                path,
                                raw_path,
                                query_string,
                                headers,
                                client,
                                scheme,
                                host,
                                path_params: Vec::new(),
                            };

                            // Deferred-upgrade: the handler returns the Response
                            // (101 or 500) and wires up the upgrade callback internally.
                            handle_ws_upgrade(ws, h, is_a, scope).await
                        }
                    },
                ),
            );
            continue;
        }

        // Pre-compute flags at startup to avoid per-request scanning
        let has_body = route.params.iter().any(|p| p.kind == "body");
        let has_header = route.params.iter().any(|p| p.kind == "header" || p.kind == "cookie");
        let has_dep = route.params.iter().any(|p| p.kind == "dependency");
        let has_any = !route.params.is_empty();
        let has_file = route.params.iter().any(|p| p.kind == "file");
        let has_form = route.params.iter().any(|p| p.kind == "form");
        let has_inj_req = route.params.iter().any(|p| p.kind == "inject_request");
        let has_inj_bg = route.params.iter().any(|p| p.kind == "inject_background_tasks");
        let has_inj_resp = route.params.iter().any(|p| p.kind == "inject_response");

        let state = Python::attach(|py| {
            // Pre-cache pydantic validators at startup (saves ~0.3μs getattr per POST request)
            let mut params = route.params.clone();
            for param in &mut params {
                if param.kind == "body" {
                    if let Some(ref model_cls) = param.model_class {
                        if let Ok(validator) = model_cls.getattr(py, "__pydantic_validator__") {
                            param.cached_validator = Some(validator);
                        }
                    }
                }
            }
            Arc::new(RouteState {
                handler: route.handler.clone_ref(py),
                params,
                is_async: route.is_async,
                has_body_params: has_body,
                has_header_params: has_header,
                has_dep_params: has_dep,
                has_any_params: has_any,
                has_inject_request: has_inj_req,
                has_inject_background_tasks: has_inj_bg,
                has_inject_response: has_inj_resp,
                has_file_params: has_file,
                has_form_params: has_form,
                has_http_middleware: route.handler
                    .getattr(py, "_has_http_middleware")
                    .and_then(|v| v.extract::<bool>(py))
                    .unwrap_or(false),
            })
        });

        let mut method_router: Option<MethodRouter> = None;
        // Track which methods this route declares (for Allow header + auto-OPTIONS).
        let declared_methods: Vec<String> = route
            .methods
            .iter()
            .map(|m| m.to_uppercase())
            .collect();
        let has_explicit_options = declared_methods.iter().any(|m| m == "OPTIONS");

        for method_str in &route.methods {
            let s = state.clone();
            let m = method_str.to_uppercase();

            let handler_fn = move |
                path_params: Option<Path<HashMap<String, String>>>,
                query_params: Query<HashMap<String, String>>,
                request: Request<Body>,
            | {
                let state = s.clone(); // Arc::clone — just refcount, no GIL
                async move {
                    handle_request(state, path_params, query_params, request).await
                }
            };

            let mr = match m.as_str() {
                "GET" => get(handler_fn),
                "POST" => post(handler_fn),
                "PUT" => put(handler_fn),
                "DELETE" => delete(handler_fn),
                "PATCH" => patch(handler_fn),
                "HEAD" => head(handler_fn),
                "OPTIONS" => axum::routing::options(handler_fn),
                other => {
                    eprintln!("fastapi-rs: unsupported HTTP method '{other}', skipping");
                    continue;
                }
            };

            method_router = Some(match method_router {
                Some(existing) => existing.merge(mr),
                None => mr,
            });
        }

        if let Some(mr) = method_router {
            // Merge with any existing accumulator for this path so that
            // `@app.get("/x")` and `@app.post("/x")` end up on one MethodRouter.
            if let Some(entry) = by_path.iter_mut().find(|(p, _, _, _)| p == &axum_path) {
                let merged = std::mem::replace(&mut entry.1, MethodRouter::new()).merge(mr);
                entry.1 = merged;
                entry.2.extend(declared_methods);
                entry.3 = entry.3 || has_explicit_options;
            } else {
                by_path.push((axum_path, mr, declared_methods, has_explicit_options));
            }
        }
    }

    // Attach 405 fallback per path. Matches FastAPI/Starlette exactly:
    //   - body: {"detail": "Method Not Allowed"}
    //   - Allow header: only the explicitly-declared methods (no auto-HEAD,
    //     no auto-OPTIONS). FastAPI expects this behaviour.
    //   - OPTIONS and HEAD on a GET-only route both return 405, matching
    //     Starlette. If the user wants OPTIONS (CORS preflight), they should
    //     mount CORSMiddleware or declare OPTIONS explicitly.
    for (path, mut mr, declared, _had_options) in by_path {
        // Dedupe while preserving order
        let mut seen = std::collections::HashSet::new();
        let mut allow: Vec<String> = declared.clone();
        allow.retain(|m| seen.insert(m.clone()));
        let allow_header = allow.join(", ");

        // FastAPI-parity: HEAD should NOT auto-route to GET. Axum's default
        // behaviour is to fall through to GET when no HEAD handler is set;
        // we override with an explicit 405 unless HEAD was declared.
        if declared.iter().any(|m| m == "GET") && !declared.iter().any(|m| m == "HEAD") {
            let h = allow_header.clone();
            mr = mr.head(move || {
                let h = h.clone();
                async move {
                    axum::response::Response::builder()
                        .status(StatusCode::METHOD_NOT_ALLOWED)
                        .header("content-type", "application/json")
                        .header("allow", h)
                        .body(axum::body::Body::from(r#"{"detail":"Method Not Allowed"}"#))
                        .unwrap()
                }
            });
        }

        let mr = mr.fallback(move || {
            let h = allow_header.clone();
            async move {
                axum::response::Response::builder()
                    .status(StatusCode::METHOD_NOT_ALLOWED)
                    .header("content-type", "application/json")
                    .header("allow", h)
                    .body(axum::body::Body::from(r#"{"detail":"Method Not Allowed"}"#))
                    .unwrap()
            }
        });
        router = router.route(&path, mr);
    }

    (router, ws_router)
}

/// Public entry: attach the FastAPI-style 404 fallback to a router. Called at
/// the top level after middleware and WS branches have been merged, so the
/// fallback fires only when nothing else matched.
pub fn with_not_found_fallback(router: Router) -> Router {
    router.fallback(|req: axum::http::Request<axum::body::Body>| async move {
        dispatch_404(req).await
    })
}

/// Python callable supplied by ``run_server(not_found_handler=...)``.
/// Expected signature: ``fn(method: str, path: str) -> bytes`` where the
/// returned bytes is a ready-to-send JSON body. The shim in
/// ``applications.py`` wraps the user's handler into this shape.
pub static NOT_FOUND_HANDLER: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();

async fn dispatch_404(req: axum::http::Request<axum::body::Body>) -> Response {
    if NOT_FOUND_HANDLER.get().is_some() {
        let path = req.uri().path().to_string();
        let method = req.method().as_str().to_string();
        let out = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> Option<(u16, Vec<u8>)> {
                let handler = NOT_FOUND_HANDLER.get()?;
                let result = handler
                    .call1(py, (method.as_str(), path.as_str()))
                    .ok()?;
                // Expected shape: (status: int, body: bytes).
                if let Ok(tup) = result.extract::<(u16, Vec<u8>)>(py) {
                    Some(tup)
                } else if let Ok(bytes) = result.extract::<Vec<u8>>(py) {
                    Some((404, bytes))
                } else {
                    None
                }
            })
        })
        .await
        .ok()
        .flatten();
        if let Some((status, body)) = out {
            return axum::response::Response::builder()
                .status(StatusCode::from_u16(status).unwrap_or(StatusCode::NOT_FOUND))
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body))
                .unwrap();
        }
    }
    axum::response::Response::builder()
        .status(StatusCode::NOT_FOUND)
        .header("content-type", "application/json")
        .body(axum::body::Body::from(r#"{"detail":"Not Found"}"#))
        .unwrap()
}

// ── Request handler (HOT PATH — optimized for minimal GIL acquisitions) ──

async fn handle_request(
    state: Arc<RouteState>,
    path_params: Option<Path<HashMap<String, String>>>,
    Query(query_params): Query<HashMap<String, String>>,
    request: Request<Body>,
) -> Response {
    // Parse raw query string into a multimap so repeated `?tag=a&tag=b`
    // keys are preserved (used when a handler param is annotated as a list).
    let query_multi: HashMap<String, Vec<String>> = {
        let mut m: HashMap<String, Vec<String>> = HashMap::new();
        for (k, v) in url::form_urlencoded::parse(
            request.uri().query().unwrap_or("").as_bytes(),
        ) {
            m.entry(k.into_owned()).or_insert_with(Vec::new).push(v.into_owned());
        }
        m
    };
    // === Pure Rust work — no GIL needed ===

    // For file/form params inspect Content-Type once. We support three body
    // shapes for these: `multipart/form-data`, `application/x-www-form-urlencoded`,
    // or plain JSON. Detection here is just reading the header value.
    #[derive(Copy, Clone, PartialEq, Eq)]
    enum FormKind { None, Multipart, UrlEncoded }

    let (multipart_boundary, form_kind): (Option<String>, FormKind) =
        if state.has_file_params || state.has_form_params {
            if let Some(ct) = request.headers().get("content-type").and_then(|v| v.to_str().ok()) {
                if let Some(b) = parse_boundary(ct) {
                    (Some(b), FormKind::Multipart)
                } else if ct.to_ascii_lowercase().starts_with("application/x-www-form-urlencoded") {
                    (None, FormKind::UrlEncoded)
                } else {
                    (None, FormKind::None)
                }
            } else {
                (None, FormKind::None)
            }
        } else {
            (None, FormKind::None)
        };

    // Always capture the full header map. Needed for:
    // - Header/Cookie params
    // - Request injection (vLLM/SGLang read request.headers)
    // - BaseHTTPMiddleware dispatch (Qwen auth reads authorization header)
    // The clone is ~2-3μs for typical request headers.
    let headers: Option<HeaderMap> = if true {
        Some(request.headers().clone())
    } else {
        None
    };

    // Capture method/path/query for Request injection AND for middleware
    // that inspects request.url.path (e.g., Qwen's BasicAuthMiddleware).
    // Always captured — the 3 string copies cost <1μs.
    let scope_method = Some(request.method().as_str().to_string());
    let scope_path = Some(request.uri().path().to_string());
    let scope_query = Some(request.uri().query().unwrap_or("").to_string());

    // Only read body if we have body/file/form params — OR if the handler
    // injects `Request`, which vLLM uses to parse bodies manually via
    // `await request.body()` / `await request.json()`.
    let needs_body = state.has_body_params
        || state.has_file_params
        || state.has_form_params
        || state.has_inject_request;

    let (body_bytes, body_json, mut multipart_fields): (
        bytes::Bytes,
        Option<serde_json::Value>,
        Option<HashMap<String, Vec<ParsedField>>>,
    ) = if needs_body {
        let bb = match axum::body::to_bytes(request.into_body(), 10 * 1024 * 1024).await {
            Ok(b) => b,
            Err(e) => {
                return (StatusCode::BAD_REQUEST, format!("Failed to read body: {e}")).into_response();
            }
        };

        // Multipart path: parse into named fields
        if let Some(ref boundary) = multipart_boundary {
            match parse_multipart(bb.clone(), boundary).await {
                Ok(fields) => (bytes::Bytes::new(), None, Some(fields)),
                Err(e) => {
                    return (StatusCode::BAD_REQUEST, format!("multipart parse: {e}"))
                        .into_response();
                }
            }
        } else if form_kind == FormKind::UrlEncoded {
            // application/x-www-form-urlencoded — convert to ParsedField map
            // so the "form" extraction path below works uniformly.
            let mut fields: HashMap<String, Vec<ParsedField>> = HashMap::new();
            for (k, v) in url::form_urlencoded::parse(&bb) {
                fields.entry(k.to_string()).or_insert_with(Vec::new).push(
                    ParsedField {
                        name: k.to_string(),
                        filename: None,
                        content_type: None,
                        data: bytes::Bytes::from(v.into_owned().into_bytes()),
                        headers: Vec::new(),
                    },
                );
            }
            (bytes::Bytes::new(), None, Some(fields))
        } else {
            // JSON / raw bytes body path (existing behavior)
            let all_have_models = state.params.iter()
                .filter(|p| p.kind == "body")
                .all(|p| p.cached_validator.is_some() || p.model_class.is_some());
            let json = if all_have_models || bb.is_empty() {
                None
            } else {
                serde_json::from_slice(&bb).ok()
            };
            (bb, json, None)
        }
    } else {
        drop(request);
        (bytes::Bytes::new(), None, None)
    };

    let path_map = path_params.map(|Path(m)| m).unwrap_or_default();

    // === Fast path: sync handler with NO dependencies ===
    // Do everything in a SINGLE block_in_place → with_gil (1 GIL acquisition, no thread hop)
    if !state.is_async && !state.has_dep_params {
        if !state.has_any_params {
            if state.has_http_middleware {
                // Middleware wrapper needs metadata kwargs
                return Python::attach(|py| {
                    let kwargs = PyDict::new(py);
                    if state.has_http_middleware { inject_request_metadata(py, &kwargs, &scope_method, &scope_path, &scope_query, &headers); }
                    match state.handler.call(py, (), Some(&kwargs)) {
                        Ok(py_result) => py_to_response(py, py_result.bind(py)),
                        Err(py_err) => pyerr_to_response(py, &py_err),
                    }
                });
            }
            // Ultra-fast path: zero-param, no middleware
            return Python::attach(|py| {
                match state.handler.call0(py) {
                    Ok(py_result) => py_to_response(py, py_result.bind(py)),
                    Err(py_err) => pyerr_to_response(py, &py_err),
                }
            });
        }

        // Sync handler with params — use block_in_place for GIL-safe concurrency
        return tokio::task::block_in_place(|| {
            Python::attach(|py| {
                let body_json_opt = if state.has_body_params { body_json.as_ref() } else { None };
                let kwargs = match extract_params_to_pydict_full(
                    py, &state.params, &path_map, &query_params, &query_multi,
                    &headers, &body_json_opt, &body_bytes, &mut multipart_fields,
                ) {
                    Ok(kw) => kw,
                    Err(resp) => return resp,
                };
                if let Err(e) = inject_framework_objects(
                    py, &kwargs, &state,
                    &scope_method, &scope_path, &scope_query,
                    &headers, &path_map, &query_params,
                    &body_bytes,
                ) {
                    return pyerr_to_response(py, &e);
                }
                if state.has_http_middleware { inject_request_metadata(py, &kwargs, &scope_method, &scope_path, &scope_query, &headers); }
                match state.handler.call(py, (), Some(&kwargs)) {
                    Ok(py_result) => {
                        drain_background_tasks(py, &kwargs, &state.params);
                        let mut resp = py_to_response(py, py_result.bind(py));
                        apply_injected_response(py, &kwargs, &state.params, &mut resp);
                        resp
                    }
                    Err(py_err) => pyerr_to_response(py, &py_err),
                }
            })
        });
    }

    // === Async fast path: run on per-thread event loop (Granian pattern) ===
    // For async handlers (with or without deps), run via loop.run_until_complete()
    // on a thread-local event loop. This eliminates the ~100-150μs cross-thread
    // overhead of run_coroutine_threadsafe. All DB awaits resolve on THIS thread.
    if state.is_async {
        return tokio::task::block_in_place(|| {
            Python::attach(|py| {
                // Build kwargs from params
                let body_json_opt = if state.has_body_params { body_json.as_ref() } else { None };

                if !state.has_any_params {
                    let kwargs = PyDict::new(py);
                    if state.has_http_middleware {
                        if state.has_http_middleware { inject_request_metadata(py, &kwargs, &scope_method, &scope_path, &scope_query, &headers); }
                    }
                    match crate::handler_bridge::call_async_on_local_loop(
                        py, &state.handler, &kwargs,
                    ) {
                        Ok(r) => py_to_response(py, r.bind(py)),
                        Err(e) => pyerr_to_response(py, &e),
                    }
                } else if !state.has_dep_params {
                    let kwargs = match extract_params_to_pydict_full(
                        py, &state.params, &path_map, &query_params, &query_multi,
                        &headers, &body_json_opt, &body_bytes, &mut multipart_fields,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py, &kwargs, &state,
                        &scope_method, &scope_path, &scope_query,
                        &headers, &path_map, &query_params,
                        &body_bytes,
                    ) {
                        return pyerr_to_response(py, &e);
                    }
                    if state.has_http_middleware { inject_request_metadata(py, &kwargs, &scope_method, &scope_path, &scope_query, &headers); }
                    match crate::handler_bridge::call_async_on_local_loop(
                        py, &state.handler, &kwargs,
                    ) {
                        Ok(r) => {
                            drain_background_tasks(py, &kwargs, &state.params);
                            let mut resp = py_to_response(py, r.bind(py));
                            apply_injected_response(py, &kwargs, &state.params, &mut resp);
                            resp
                        }
                        Err(e) => pyerr_to_response(py, &e),
                    }
                } else {
                    let kwargs = match extract_params_to_pydict_full(
                        py, &state.params, &path_map, &query_params, &query_multi,
                        &headers, &body_json_opt, &body_bytes, &mut multipart_fields,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py, &kwargs, &state,
                        &scope_method, &scope_path, &scope_query,
                        &headers, &path_map, &query_params,
                        &body_bytes,
                    ) {
                        return pyerr_to_response(py, &e);
                    }
                    if state.has_http_middleware { inject_request_metadata(py, &kwargs, &scope_method, &scope_path, &scope_query, &headers); }
                    match state.handler.call(py, (), Some(&kwargs)) {
                        Ok(r) => {
                            drain_background_tasks(py, &kwargs, &state.params);
                            py_to_response(py, r.bind(py))
                        }
                        Err(e) => pyerr_to_response(py, &e),
                    }
                }
            })
        });
    }

    // === Unified path: sync handlers with dependencies ===
    let resp = tokio::task::block_in_place(|| {
        Python::attach(|py| -> Response {
            let mut resolved: HashMap<String, Py<PyAny>> = HashMap::new();
            let mut dep_cache: HashMap<u64, String> = HashMap::new();

            for param in &state.params {
                match param.kind.as_str() {
                    "dependency" => {
                        // Check cache first
                        if let Some(func_id) = param.dep_callable_id {
                            if let Some(cached_key) = dep_cache.get(&func_id) {
                                if let Some(cached_val) = resolved.get(cached_key) {
                                    resolved.insert(param.name.clone(), cached_val.clone_ref(py));
                                    continue;
                                }
                            }
                        }

                        let Some(ref dep_callable) = param.dep_callable else { continue };

                        // Build kwargs for this dep from previously resolved values
                        let dep_kwargs = PyDict::new(py);
                        for (param_name, source_key) in &param.dep_input_names {
                            if let Some(val) = resolved.get(source_key) {
                                let _ = dep_kwargs.set_item(param_name, val.bind(py));
                            }
                        }

                        // Call the dep — try synchronous completion via send(None)
                        let result = if param.is_async_dep {
                            try_call_async_sync(py, dep_callable, &dep_kwargs)
                        } else {
                            dep_callable.call(py, (), Some(&dep_kwargs)).map_err(|e| e)
                        };

                        match result {
                            Ok(val) => {
                                if let Some(func_id) = param.dep_callable_id {
                                    dep_cache.insert(func_id, param.name.clone());
                                }
                                resolved.insert(param.name.clone(), val);
                            }
                            Err(py_err) => return pyerr_to_response(py, &py_err),
                        }
                    }
                    _ => {
                        // Extract non-dep params
                        if let Err(resp) = extract_single_param(
                            py, param, &path_map, &query_params, &headers, &body_json, &mut resolved,
                        ) {
                            return resp;
                        }
                    }
                }
            }

            // Build handler kwargs
            let kwargs = PyDict::new(py);
            for param in &state.params {
                if param.is_handler_param {
                    if let Some(val) = resolved.get(&param.name) {
                        let _ = kwargs.set_item(&param.name, val.bind(py));
                    }
                }
            }

            // Call handler — try sync completion for async handlers too
            let result = if state.is_async {
                try_call_async_sync(py, &state.handler, &kwargs)
            } else {
                state.handler.call(py, (), Some(&kwargs))
            };

            match result {
                Ok(py_result) => py_to_response(py, py_result.bind(py)),
                Err(ref py_err) => {
                    // Check if this is "needs event loop" — signal for fallback
                    let msg = py_err.value(py).str().map(|s| s.to_string()).unwrap_or_default();
                    if msg.contains("event loop") {
                        // Return a sentinel status to signal fallback needed
                        (StatusCode::from_u16(599).unwrap(), "NEEDS_EVENT_LOOP").into_response()
                    } else {
                        pyerr_to_response(py, &py_err)
                    }
                }
            }
        })
    });

    // Check if the unified path signaled that an event loop is needed
    if resp.status() == StatusCode::from_u16(599).unwrap() {
        // Fall back to event-loop-based async execution
        let handler_kwargs: HashMap<String, Py<PyAny>> = Python::attach(|py| {
            let mut hk = HashMap::new();
            // Re-extract all params (we lost them in the unified path)
            // This is the slow path — only triggers for truly async handlers (asyncio.sleep etc.)
            let mut resolved: HashMap<String, Py<PyAny>> = HashMap::new();
            for param in &state.params {
                if param.kind != "dependency" {
                    let _ = extract_single_param(py, param, &path_map, &query_params, &headers, &body_json, &mut resolved);
                }
            }
            // Re-resolve deps via the old approach won't work here...
            // Just build kwargs from what we can
            for param in &state.params {
                if param.is_handler_param {
                    if let Some(val) = resolved.get(&param.name) {
                        hk.insert(param.name.clone(), val.clone_ref(py));
                    }
                }
            }
            hk
        });

        let handler = Python::attach(|py| state.handler.clone_ref(py));
        return match call_async_handler(handler, handler_kwargs).await {
            Ok(py_result) => Python::attach(|py| py_to_response(py, py_result.bind(py))),
            Err(py_err) => Python::attach(|py| pyerr_to_response(py, &py_err)),
        };
    }

    resp
}

// ── Async try-sync helper ────────────────────────────────────────────

/// Try to call an async Python function synchronously via coro.send(None).
/// If the coroutine completes immediately (StopIteration), returns the value.
/// If it suspends or needs an event loop, returns an error.
fn try_call_async_sync(
    py: Python<'_>,
    handler: &Py<PyAny>,
    kwargs: &pyo3::Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    let coro = handler.call(py, (), Some(kwargs))?;
    match coro.call_method1(py, "send", (py.None(),)) {
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
            // Completed synchronously — extract value
            match e.value(py).getattr("value") {
                Ok(val) => Ok(val.unbind()),
                Err(_) => Ok(py.None()),
            }
        }
        Err(e) => {
            // Check if it's a "no running event loop" error — treat same as suspension
            let is_runtime = e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py);
            let msg = e.value(py).str().map(|s| s.to_string()).unwrap_or_default();
            if is_runtime && msg.contains("event loop") {
                let _ = coro.call_method0(py, "close");
                // TODO: fall back to event loop bridge for truly async handlers
                Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "Handler requires a running event loop (asyncio.sleep, etc.). Use sync deps for best performance.",
                ))
            } else {
                Err(e)
            }
        }
        Ok(_) => {
            let _ = coro.call_method0(py, "close");
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Coroutine suspended — requires event loop",
            ))
        }
    }
}

// ── Parameter extraction helpers ─────────────────────────────────────

/// Extract ALL params directly into a PyDict (fast path for sync handlers without deps).
/// Single GIL acquisition — no intermediate HashMap.
fn extract_params_to_pydict<'py>(
    py: Python<'py>,
    params: &[ParamInfo],
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    headers: &Option<HeaderMap>,
    body_json: &Option<&serde_json::Value>,
    body_bytes: &[u8],
    multipart_fields: &mut Option<HashMap<String, Vec<ParsedField>>>,
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    extract_params_to_pydict_full(
        py, params, path_map, query_params, &HashMap::new(),
        headers, body_json, body_bytes, multipart_fields,
    )
}

fn extract_params_to_pydict_full<'py>(
    py: Python<'py>,
    params: &[ParamInfo],
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    query_multi: &HashMap<String, Vec<String>>,
    headers: &Option<HeaderMap>,
    body_json: &Option<&serde_json::Value>,
    body_bytes: &[u8],
    multipart_fields: &mut Option<HashMap<String, Vec<ParsedField>>>,
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    let kwargs = pyo3::types::PyDict::new(py);

    for param in params {
        if !param.is_handler_param { continue; }

        match param.kind.as_str() {
            "path" => {
                if let Some(raw) = path_map.get(&param.name) {
                    match try_coerce_str_to_py(py, raw, &param.type_hint) {
                        Some(v) => {
                            let bound = v.bind(py);
                            let validated = run_scalar_validator(py, param, "path", bound)?;
                            let _ = kwargs.set_item(&param.name, validated);
                        }
                        None => return Err(coercion_error_response("path", &param.name, raw, &param.type_hint)),
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    return Err(validation_error_response("path", &param.name, "field required"));
                }
            }
            "query" => {
                // List types collect ALL values for repeated `?k=a&k=b`
                if param.type_hint.starts_with("list_") {
                    let values = query_multi
                        .get(&param.name)
                        .cloned()
                        .unwrap_or_default();
                    if values.is_empty() {
                        if !apply_default(py, &kwargs, param) && param.required {
                            return Err(validation_error_response("query", &param.name, "field required"));
                        }
                    } else {
                        let inner = &param.type_hint[5..]; // strip "list_"
                        let list = pyo3::types::PyList::empty(py);
                        for v in &values {
                            match try_coerce_str_to_py(py, v, inner) {
                                Some(coerced) => { let _ = list.append(coerced.bind(py)); }
                                None => return Err(coercion_error_response("query", &param.name, v, inner)),
                            }
                        }
                        let _ = kwargs.set_item(&param.name, list);
                    }
                } else if let Some(raw) = query_params.get(&param.name) {
                    match try_coerce_str_to_py(py, raw, &param.type_hint) {
                        Some(v) => {
                            let bound = v.bind(py);
                            let validated = run_scalar_validator(py, param, "query", bound)?;
                            let _ = kwargs.set_item(&param.name, validated);
                        }
                        None => return Err(coercion_error_response("query", &param.name, raw, &param.type_hint)),
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    return Err(validation_error_response("query", &param.name, "field required"));
                }
            }
            "body" => {
                if !body_bytes.is_empty() {
                    let val = if param.cached_validator.is_some() || param.model_class.is_some() {
                        // Use cached SchemaValidator.validate_json(bytes) — zero getattr, one call
                        let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                        let result = if let Some(ref validator) = param.cached_validator {
                            validator.call_method1(py, "validate_json", (py_bytes,))
                        } else {
                            // Fallback: getattr if cache missed
                            param.model_class.as_ref().unwrap()
                                .getattr(py, "__pydantic_validator__")
                                .and_then(|v| v.call_method1(py, "validate_json", (py_bytes,)))
                        };
                        match result {
                            Ok(v) => v,
                            Err(e) => {
                                return Err(pydantic_error_response(py, &e, "body"));
                            }
                        }
                    } else if let Some(ref json_val) = body_json {
                        // No Pydantic model — pass as dict
                        serde_to_pyobj(py, json_val)
                    } else {
                        // Raw bytes couldn't be parsed as JSON
                        let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                        py_bytes.into_any().unbind()
                    };
                    let _ = kwargs.set_item(&param.name, val.bind(py));
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    return Err(validation_error_response("body", &param.name, "field required"));
                }
            }
            "header" => {
                let lookup = param.alias.as_deref().unwrap_or(&param.name).to_lowercase();
                let header_val = headers.as_ref()
                    .and_then(|h| h.get(lookup.as_str()))
                    .and_then(|v| v.to_str().ok());
                if let Some(raw) = header_val {
                    match try_coerce_str_to_py(py, raw, &param.type_hint) {
                        Some(v) => { let _ = kwargs.set_item(&param.name, v.bind(py)); }
                        None => return Err(coercion_error_response("header", &param.name, raw, &param.type_hint)),
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    return Err(validation_error_response("header", &param.name, "field required"));
                }
            }
            "cookie" => {
                // Cookie lookup uses `alias` when set (e.g., APIKeyCookie
                // wraps its value in `Cookie(alias="sessionid")`), else
                // the Python parameter name.
                let lookup = param.alias.as_deref().unwrap_or(&param.name);
                let cookie_val = headers.as_ref()
                    .and_then(|h| h.get("cookie"))
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| parse_cookie_value(s, lookup));
                if let Some(raw) = cookie_val {
                    match try_coerce_str_to_py(py, &raw, &param.type_hint) {
                        Some(v) => { let _ = kwargs.set_item(&param.name, v.bind(py)); }
                        None => return Err(coercion_error_response("cookie", &param.name, &raw, &param.type_hint)),
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    return Err(validation_error_response("cookie", &param.name, "field required"));
                }
            }
            "file" => {
                // Multipart file param — wrap each PyUploadFile in the Python
                // `UploadFile` class so handlers can `await file.read()`.
                let fields = multipart_fields
                    .as_mut()
                    .and_then(|m| m.remove(&param.name));
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        if fs.len() == 1 {
                            let wrapped = make_upload_file(py, fs.remove(0)).map_err(|_e| {
                                validation_error_response("file", &param.name, "alloc")
                            })?;
                            let _ = kwargs.set_item(&param.name, wrapped);
                        } else {
                            let list = pyo3::types::PyList::empty(py);
                            for f in fs {
                                let wrapped = make_upload_file(py, f).map_err(|_e| {
                                    validation_error_response("file", &param.name, "alloc")
                                })?;
                                let _ = list.append(wrapped);
                            }
                            let _ = kwargs.set_item(&param.name, list);
                        }
                    }
                    _ => {
                        if let Some(ref default) = param.default_value {
                            let _ = kwargs.set_item(&param.name, default.bind(py));
                        } else if param.required {
                            return Err(validation_error_response("file", &param.name, "field required"));
                        }
                    }
                }
            }
            "form" => {
                // Multipart form field — could be a plain string OR a file.
                // If it has a filename, treat as UploadFile; else as str/int/etc.
                let fields = multipart_fields
                    .as_mut()
                    .and_then(|m| m.remove(&param.name));
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        let field = fs.remove(0);
                        if field.filename.is_some() {
                            let wrapped = make_upload_file(py, field).map_err(|_e| {
                                validation_error_response("form", &param.name, "alloc")
                            })?;
                            let _ = kwargs.set_item(&param.name, wrapped);
                        } else {
                            let text = String::from_utf8_lossy(&field.data).into_owned();
                            let _ = kwargs.set_item(
                                &param.name,
                                coerce_str_to_py(py, &text, &param.type_hint).bind(py),
                            );
                        }
                    }
                    _ => {
                        if let Some(ref default) = param.default_value {
                            let _ = kwargs.set_item(&param.name, default.bind(py));
                        } else if param.required {
                            return Err(validation_error_response("form", &param.name, "field required"));
                        }
                    }
                }
            }
            // Special framework-provided injections — resolved entirely
            // in Python so we just pass a sentinel marker here; the Python
            // handler wrapper will substitute the real Request /
            // BackgroundTasks / Response object.
            "inject_request"
            | "inject_background_tasks"
            | "inject_response"
            | "inject_websocket"
            | "inject_security_scopes" => {
                // Leave unset — injected in `inject_framework_objects`.
            }
            _ => {}
        }
    }

    Ok(kwargs)
}

/// Extract a single param into the resolved HashMap (slow path for dep handlers).
fn extract_single_param(
    py: Python<'_>,
    param: &ParamInfo,
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    headers: &Option<HeaderMap>,
    body_json: &Option<serde_json::Value>,
    resolved: &mut HashMap<String, Py<PyAny>>,
) -> Result<(), Response> {
    match param.kind.as_str() {
        "path" => {
            if let Some(raw) = path_map.get(&param.name) {
                resolved.insert(param.name.clone(), coerce_str_to_py(py, raw, &param.type_hint));
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                return Err(validation_error_response("path", &param.name, "field required"));
            }
        }
        "query" => {
            if let Some(raw) = query_params.get(&param.name) {
                resolved.insert(param.name.clone(), coerce_str_to_py(py, raw, &param.type_hint));
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                return Err(validation_error_response("query", &param.name, "field required"));
            }
        }
        "body" => {
            if let Some(ref json_val) = body_json {
                let raw_dict = serde_to_pyobj(py, json_val);
                let val = if let Some(ref model_cls) = param.model_class {
                    model_cls.call_method1(py, "model_validate", (raw_dict.bind(py),))
                        .map_err(|e| validation_error_response("body", &param.name, &format!("{e}")))?
                } else {
                    raw_dict
                };
                resolved.insert(param.name.clone(), val);
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                return Err(validation_error_response("body", &param.name, "field required"));
            }
        }
        "header" => {
            let lookup = param.alias.as_deref().unwrap_or(&param.name).to_lowercase();
            let header_val = headers.as_ref()
                .and_then(|h| h.get(lookup.as_str()))
                .and_then(|v| v.to_str().ok());
            if let Some(raw) = header_val {
                resolved.insert(param.name.clone(), coerce_str_to_py(py, raw, &param.type_hint));
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                return Err(validation_error_response("header", &param.name, "field required"));
            }
        }
        "cookie" => {
            let cookie_val = headers.as_ref()
                .and_then(|h| h.get("cookie"))
                .and_then(|v| v.to_str().ok())
                .and_then(|s| parse_cookie_value(s, &param.name));
            if let Some(raw) = cookie_val {
                resolved.insert(param.name.clone(), coerce_str_to_py(py, &raw, &param.type_hint));
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                return Err(validation_error_response("cookie", &param.name, "field required"));
            }
        }
        _ => {}
    }
    Ok(())
}

// ── Helpers ───────────────────────────────────────────────────────────

/// Build a 422 response for a "Field required" (missing) validation error,
/// in Pydantic-v2 / FastAPI format.
fn validation_error_response(loc: &str, name: &str, _msg: &str) -> Response {
    let body = serde_json::json!({
        "detail": [{
            "type": "missing",
            "loc": [loc, name],
            "msg": "Field required",
            "input": serde_json::Value::Null,
        }]
    });
    dispatch_validation_error(body)
}

/// Return a 422 response. When the app has registered a handler for
/// `RequestValidationError`, the detail is passed to Python so the user's
/// handler shapes the final body. Otherwise, the default JSON body is used.
pub fn dispatch_validation_error(detail_json: serde_json::Value) -> Response {
    if let Some(handler) = VALIDATION_HANDLER.get() {
        let result: PyResult<(u16, Vec<u8>, String)> = Python::attach(|py| {
            let s = detail_json.to_string();
            let ret = handler.call1(py, (s,))?;
            let t = ret.bind(py);
            let status: u16 = t.get_item(0)?.extract()?;
            let body_bytes: Vec<u8> = t.get_item(1)?.extract()?;
            let ct: String = t
                .get_item(2)
                .ok()
                .and_then(|v| v.extract().ok())
                .unwrap_or_else(|| "application/json".to_string());
            Ok((status, body_bytes, ct))
        });
        if let Ok((status, body_bytes, ct)) = result {
            let status =
                StatusCode::from_u16(status).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY);
            return Response::builder()
                .status(status)
                .header("content-type", ct)
                .body(axum::body::Body::from(body_bytes))
                .unwrap();
        }
    }
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        [("content-type", "application/json")],
        detail_json.to_string(),
    )
        .into_response()
}

/// Build a 422 response for a str→type coercion failure (int_parsing, etc.),
/// in Pydantic-v2 format.
fn coercion_error_response(loc: &str, name: &str, raw: &str, type_hint: &str) -> Response {
    let (err_type, msg) = match type_hint {
        "int" => ("int_parsing", "Input should be a valid integer, unable to parse string as an integer"),
        "float" => ("float_parsing", "Input should be a valid number, unable to parse string as a number"),
        "bool" => ("bool_parsing", "Input should be a valid boolean, unable to interpret input"),
        _ => ("value_error", "Value error"),
    };
    let body = serde_json::json!({
        "detail": [{
            "type": err_type,
            "loc": [loc, name],
            "msg": msg,
            "input": raw,
        }]
    });
    dispatch_validation_error(body)
}

/// Convert a Pydantic ValidationError (from body model validation) into a
/// FastAPI-style 422 response. Prepends `loc_prefix` (e.g. "body") to each
/// error's location.
fn pydantic_error_response(py: Python<'_>, err: &PyErr, loc_prefix: &str) -> Response {
    // Access the ValidationError object and call .errors()
    let err_obj = err.value(py);
    let errors_method = match err_obj.getattr("errors") {
        Ok(m) => m,
        Err(_) => {
            // Not a ValidationError — fall back to generic
            return validation_error_response(loc_prefix, "", &format!("{err}"));
        }
    };
    let errors_list = match errors_method.call0() {
        Ok(l) => l,
        Err(_) => return validation_error_response(loc_prefix, "", &format!("{err}")),
    };

    let mut details = Vec::new();
    if let Ok(list) = errors_list.cast::<pyo3::types::PyList>() {
        for item in list.iter() {
            if let Ok(d) = item.cast::<PyDict>() {
                let err_type_str = d
                    .get_item("type")
                    .ok()
                    .flatten()
                    .and_then(|v| v.extract::<String>().ok());

                // FastAPI/Starlette surface pydantic's `json_invalid` with a
                // canonical shape that differs from pydantic's raw output. In
                // particular `ctx.error` must match Python's stdlib json error
                // message (e.g. "Expecting value"), not pydantic-core's Rust
                // message ("expected ident"). We re-parse the input with
                // Python's `json.loads` to get the matching message.
                if err_type_str.as_deref() == Some("json_invalid") {
                    let raw_input = d
                        .get_item("input")
                        .ok()
                        .flatten()
                        .and_then(|v| {
                            v.extract::<Vec<u8>>().ok()
                                .or_else(|| v.extract::<String>().ok().map(|s| s.into_bytes()))
                        });

                    let ctx_error = if let Some(bytes) = raw_input.as_ref() {
                        let json_mod = py.import("json");
                        let msg_opt = json_mod
                            .and_then(|j| j.call_method1("loads", (bytes.clone(),)).map(|_| String::new()).or_else(|e| {
                                let v = e.value(py);
                                let msg = v.getattr("msg")
                                    .and_then(|m| m.extract::<String>())
                                    .unwrap_or_else(|_| format!("{e}"));
                                Ok::<String, PyErr>(msg)
                            }))
                            .ok();
                        msg_opt.unwrap_or_else(|| "Expecting value".to_string())
                    } else {
                        "Expecting value".to_string()
                    };
                    let obj = serde_json::json!({
                        "type": "json_invalid",
                        "loc": [loc_prefix, 0],
                        "msg": "JSON decode error",
                        "input": {},
                        "ctx": {"error": ctx_error},
                    });
                    details.push(obj);
                    continue;
                }

                let mut obj = serde_json::Map::new();
                if let Some(t) = err_type_str {
                    obj.insert("type".into(), serde_json::Value::String(t));
                }
                // Prepend loc_prefix to location
                let mut loc = vec![serde_json::Value::String(loc_prefix.to_string())];
                if let Some(l) = d.get_item("loc").ok().flatten() {
                    if let Ok(tup) = l.cast::<pyo3::types::PyTuple>() {
                        for item in tup.iter() {
                            if let Ok(s) = item.extract::<String>() {
                                loc.push(serde_json::Value::String(s));
                            } else if let Ok(i) = item.extract::<i64>() {
                                loc.push(serde_json::Value::Number(i.into()));
                            }
                        }
                    } else if let Ok(lst) = l.cast::<pyo3::types::PyList>() {
                        for item in lst.iter() {
                            if let Ok(s) = item.extract::<String>() {
                                loc.push(serde_json::Value::String(s));
                            } else if let Ok(i) = item.extract::<i64>() {
                                loc.push(serde_json::Value::Number(i.into()));
                            }
                        }
                    }
                }
                obj.insert("loc".into(), serde_json::Value::Array(loc));
                if let Some(m) = d.get_item("msg").ok().flatten().and_then(|v| v.extract::<String>().ok()) {
                    obj.insert("msg".into(), serde_json::Value::String(m));
                }
                // input field — best-effort serialize to JSON via python's json module
                if let Some(inp) = d.get_item("input").ok().flatten() {
                    let input_val: serde_json::Value = if let Ok(s) = inp.extract::<String>() {
                        serde_json::Value::String(s)
                    } else if let Ok(b) = inp.extract::<bool>() {
                        serde_json::Value::Bool(b)
                    } else if let Ok(n) = inp.extract::<i64>() {
                        serde_json::Value::Number(n.into())
                    } else if inp.is_none() {
                        serde_json::Value::Null
                    } else {
                        // Fall back to json.dumps for dicts/lists/etc.
                        py.import("json")
                            .and_then(|j| j.call_method1("dumps", (&inp,)))
                            .and_then(|s| s.extract::<String>())
                            .ok()
                            .and_then(|s| serde_json::from_str(&s).ok())
                            .unwrap_or(serde_json::Value::Null)
                    };
                    obj.insert("input".into(), input_val);
                }
                details.push(serde_json::Value::Object(obj));
            }
        }
    }

    if details.is_empty() {
        return validation_error_response(loc_prefix, "", &format!("{err}"));
    }

    let body = serde_json::json!({ "detail": details });
    dispatch_validation_error(body)
}

fn parse_cookie_value(cookie_header: &str, name: &str) -> Option<String> {
    for pair in cookie_header.split(';') {
        let pair = pair.trim();
        if let Some((key, value)) = pair.split_once('=') {
            if key.trim() == name {
                return Some(value.trim().to_string());
            }
        }
    }
    None
}

/// Coerce a string value to a Python object of the given type.
/// Returns None on parse failure so callers can emit a 422.
fn coerce_str_to_py(py: Python<'_>, raw: &str, type_hint: &str) -> Py<PyAny> {
    try_coerce_str_to_py(py, raw, type_hint)
        .unwrap_or_else(|| raw.into_pyobject(py).expect("str").into_any().unbind())
}

/// Strict coercion: returns None when the raw string cannot be parsed as the
/// target type (rather than silently returning the raw string).
fn try_coerce_str_to_py(py: Python<'_>, raw: &str, type_hint: &str) -> Option<Py<PyAny>> {
    match type_hint {
        "int" => raw.parse::<i64>()
            .ok()
            .map(|i| i.into_pyobject(py).expect("int").into_any().unbind()),
        "float" => raw.parse::<f64>()
            .ok()
            .map(|f| f.into_pyobject(py).expect("float").into_any().unbind()),
        "bool" => {
            match raw {
                "true" | "True" | "1" | "yes" | "on" => Some(
                    pyo3::types::PyBool::new(py, true).to_owned().into_any().unbind(),
                ),
                "false" | "False" | "0" | "no" | "off" => Some(
                    pyo3::types::PyBool::new(py, false).to_owned().into_any().unbind(),
                ),
                _ => None,
            }
        }
        _ => Some(raw.into_pyobject(py).expect("str").into_any().unbind()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_convert_simple_path() {
        assert_eq!(convert_path("/users/{user_id}"), "/users/{user_id}");
    }

    #[test]
    fn test_convert_multiple_params() {
        assert_eq!(convert_path("/users/{user_id}/posts/{post_id}"), "/users/{user_id}/posts/{post_id}");
    }

    #[test]
    fn test_convert_catch_all() {
        assert_eq!(convert_path("/files/{file_path:path}"), "/files/{*file_path}");
    }

    #[test]
    fn test_convert_no_params() {
        assert_eq!(convert_path("/hello"), "/hello");
    }
}
