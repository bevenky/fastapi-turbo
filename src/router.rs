use axum::body::Body;
use axum::extract::{Path, Query, Request};
use axum::extract::ws::WebSocketUpgrade;
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{MethodRouter, any, get, post, put, delete, patch, head};
use axum::Router;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::Arc;

use crate::handler_bridge::{call_async_handler, call_sync_handler};
use crate::responses::{py_to_response, pyerr_to_response, serde_to_pyobj};
use crate::websocket::handle_ws_connection;

// ── Data types exposed to Python ──────────────────────────────────────

#[pyclass]
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
    #[pyo3(get, set)]
    pub model_class: Option<Py<PyAny>>,
    /// Cached SchemaValidator — avoids getattr("__pydantic_validator__") per-request
    pub cached_validator: Option<Py<PyAny>>,
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
        Python::with_gil(|py| ParamInfo {
            name: self.name.clone(),
            kind: self.kind.clone(),
            type_hint: self.type_hint.clone(),
            required: self.required,
            default_value: self.default_value.as_ref().map(|v| v.clone_ref(py)),
            model_class: self.model_class.as_ref().map(|v| v.clone_ref(py)),
            cached_validator: self.cached_validator.as_ref().map(|v| v.clone_ref(py)),
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
    #[pyo3(signature = (name, kind, type_hint="str".to_string(), required=true, default_value=None, model_class=None, alias=None, dep_callable=None, dep_callable_id=None, is_async_dep=false, is_generator_dep=false, dep_input_names=vec![], is_handler_param=true))]
    fn new(
        name: String,
        kind: String,
        type_hint: String,
        required: bool,
        default_value: Option<Py<PyAny>>,
        model_class: Option<Py<PyAny>>,
        alias: Option<String>,
        dep_callable: Option<Py<PyAny>>,
        dep_callable_id: Option<u64>,
        is_async_dep: bool,
        is_generator_dep: bool,
        dep_input_names: Vec<(String, String)>,
        is_handler_param: bool,
    ) -> Self {
        ParamInfo {
            name, kind, type_hint, required, default_value, model_class,
            cached_validator: None, // Populated at startup by build_router
            alias, dep_callable, dep_callable_id, is_async_dep, is_generator_dep,
            dep_input_names, is_handler_param,
        }
    }
}

#[pyclass]
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
        Python::with_gil(|py| RouteInfo {
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
    // Note: body validation stays with Pydantic (Rust-backed) for 100% compatibility.
    // jsonschema crate can't handle custom validators, coercion, defaults, etc.
}

struct WsRouteState {
    handler: Py<PyAny>,
    is_async: bool,
}

// ── Router builder ────────────────────────────────────────────────────

pub fn build_router(routes: Vec<RouteInfo>) -> Router {
    let mut router = Router::new();

    for route in routes {
        let axum_path = convert_path(&route.path);

        if route.is_websocket {
            let ws_state = Arc::new(WsRouteState {
                handler: Python::with_gil(|py| route.handler.clone_ref(py)),
                is_async: route.is_async,
            });
            router = router.route(
                &axum_path,
                any(move |ws: WebSocketUpgrade| {
                    let state = ws_state.clone();
                    async move {
                        let h = Python::with_gil(|py| state.handler.clone_ref(py));
                        let is_a = state.is_async;
                        ws.on_upgrade(move |socket| handle_ws_connection(socket, h, is_a))
                    }
                }),
            );
            continue;
        }

        // Pre-compute flags at startup to avoid per-request scanning
        let has_body = route.params.iter().any(|p| p.kind == "body");
        let has_header = route.params.iter().any(|p| p.kind == "header" || p.kind == "cookie");
        let has_dep = route.params.iter().any(|p| p.kind == "dependency");
        let has_any = !route.params.is_empty();

        let state = Python::with_gil(|py| {
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
            })
        });

        let mut method_router: Option<MethodRouter> = None;

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
            router = router.route(&axum_path, mr);
        }
    }

    router
}

// ── Request handler (HOT PATH — optimized for minimal GIL acquisitions) ──

async fn handle_request(
    state: Arc<RouteState>,
    path_params: Option<Path<HashMap<String, String>>>,
    Query(query_params): Query<HashMap<String, String>>,
    request: Request<Body>,
) -> Response {
    // === Pure Rust work — no GIL needed ===

    // Only clone headers if we actually have header/cookie params
    let headers: Option<HeaderMap> = if state.has_header_params {
        Some(request.headers().clone())
    } else {
        None
    };

    // Only read body if we have body params
    let (body_bytes, body_json): (bytes::Bytes, Option<serde_json::Value>) = if state.has_body_params {
        let bb = match axum::body::to_bytes(request.into_body(), 10 * 1024 * 1024).await {
            Ok(b) => b,
            Err(e) => {
                return (StatusCode::BAD_REQUEST, format!("Failed to read body: {e}")).into_response();
            }
        };
        // Skip Rust JSON parse if all body params have Pydantic models (they parse JSON internally)
        let all_have_models = state.params.iter()
            .filter(|p| p.kind == "body")
            .all(|p| p.cached_validator.is_some() || p.model_class.is_some());
        let json = if all_have_models || bb.is_empty() {
            None  // Pydantic handles JSON parsing — avoid double parse
        } else {
            serde_json::from_slice(&bb).ok()
        };
        (bb, json)
    } else {
        drop(request);
        (bytes::Bytes::new(), None)
    };

    let path_map = path_params.map(|Path(m)| m).unwrap_or_default();

    // === Fast path: sync handler with NO dependencies ===
    // Do everything in a SINGLE block_in_place → with_gil (1 GIL acquisition, no thread hop)
    if !state.is_async && !state.has_dep_params {
        // Ultra-fast path: zero-param handlers — no block_in_place, no PyDict, just GIL + call
        if !state.has_any_params {
            return Python::with_gil(|py| {
                match state.handler.call0(py) {
                    Ok(py_result) => py_to_response(py, py_result.bind(py)),
                    Err(py_err) => pyerr_to_response(py, &py_err),
                }
            });
        }

        // Sync handler with params — use block_in_place for GIL-safe concurrency
        return tokio::task::block_in_place(|| {
            Python::with_gil(|py| {
                let body_json_opt = if state.has_body_params { body_json.as_ref() } else { None };
                let kwargs = match extract_params_to_pydict(
                    py, &state.params, &path_map, &query_params,
                    &headers, &body_json_opt, &body_bytes,
                ) {
                    Ok(kw) => kw,
                    Err(resp) => return resp,
                };
                match state.handler.call(py, (), Some(&kwargs)) {
                    Ok(py_result) => py_to_response(py, py_result.bind(py)),
                    Err(py_err) => pyerr_to_response(py, &py_err),
                }
            })
        });
    }

    // === Unified path: async handlers and/or dependencies ===
    // Try to do EVERYTHING in a single block_in_place → with_gil (1 GIL acquisition).
    // For each async callable, use coro.send(None) to try synchronous completion.
    // Only fall back to the event loop if a coroutine truly suspends.
    let mut resp = tokio::task::block_in_place(|| {
        Python::with_gil(|py| -> Response {
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
        let handler_kwargs: HashMap<String, Py<PyAny>> = Python::with_gil(|py| {
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

        let handler = Python::with_gil(|py| state.handler.clone_ref(py));
        return match call_async_handler(handler, handler_kwargs).await {
            Ok(py_result) => Python::with_gil(|py| py_to_response(py, py_result.bind(py))),
            Err(py_err) => Python::with_gil(|py| pyerr_to_response(py, &py_err)),
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
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    let kwargs = pyo3::types::PyDict::new(py);

    for param in params {
        if !param.is_handler_param { continue; }

        match param.kind.as_str() {
            "path" => {
                if let Some(raw) = path_map.get(&param.name) {
                    let _ = kwargs.set_item(&param.name, coerce_str_to_py(py, raw, &param.type_hint).bind(py));
                } else if let Some(ref default) = param.default_value {
                    let _ = kwargs.set_item(&param.name, default.bind(py));
                } else if param.required {
                    return Err(validation_error_response("path", &param.name, "field required"));
                }
            }
            "query" => {
                if let Some(raw) = query_params.get(&param.name) {
                    let _ = kwargs.set_item(&param.name, coerce_str_to_py(py, raw, &param.type_hint).bind(py));
                } else if let Some(ref default) = param.default_value {
                    let _ = kwargs.set_item(&param.name, default.bind(py));
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
                                return Err(validation_error_response("body", &param.name, &format!("{e}")));
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
                } else if let Some(ref default) = param.default_value {
                    let _ = kwargs.set_item(&param.name, default.bind(py));
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
                    let _ = kwargs.set_item(&param.name, coerce_str_to_py(py, raw, &param.type_hint).bind(py));
                } else if let Some(ref default) = param.default_value {
                    let _ = kwargs.set_item(&param.name, default.bind(py));
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
                    let _ = kwargs.set_item(&param.name, coerce_str_to_py(py, &raw, &param.type_hint).bind(py));
                } else if let Some(ref default) = param.default_value {
                    let _ = kwargs.set_item(&param.name, default.bind(py));
                } else if param.required {
                    return Err(validation_error_response("cookie", &param.name, "field required"));
                }
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
            } else if let Some(ref default) = param.default_value {
                resolved.insert(param.name.clone(), default.clone_ref(py));
            } else if param.required {
                return Err(validation_error_response("path", &param.name, "field required"));
            }
        }
        "query" => {
            if let Some(raw) = query_params.get(&param.name) {
                resolved.insert(param.name.clone(), coerce_str_to_py(py, raw, &param.type_hint));
            } else if let Some(ref default) = param.default_value {
                resolved.insert(param.name.clone(), default.clone_ref(py));
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
            } else if let Some(ref default) = param.default_value {
                resolved.insert(param.name.clone(), default.clone_ref(py));
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
            } else if let Some(ref default) = param.default_value {
                resolved.insert(param.name.clone(), default.clone_ref(py));
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
            } else if let Some(ref default) = param.default_value {
                resolved.insert(param.name.clone(), default.clone_ref(py));
            } else if param.required {
                return Err(validation_error_response("cookie", &param.name, "field required"));
            }
        }
        _ => {}
    }
    Ok(())
}

// ── Helpers ───────────────────────────────────────────────────────────

fn validation_error_response(loc: &str, name: &str, msg: &str) -> Response {
    let body = serde_json::json!({
        "detail": [{"loc": [loc, name], "msg": msg, "type": "value_error.missing"}]
    });
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        [("content-type", "application/json")],
        body.to_string(),
    ).into_response()
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

fn coerce_str_to_py(py: Python<'_>, raw: &str, type_hint: &str) -> Py<PyAny> {
    match type_hint {
        "int" => {
            if let Ok(i) = raw.parse::<i64>() {
                i.into_pyobject(py).expect("int").into_any().unbind()
            } else {
                raw.into_pyobject(py).expect("str").into_any().unbind()
            }
        }
        "float" => {
            if let Ok(f) = raw.parse::<f64>() {
                f.into_pyobject(py).expect("float").into_any().unbind()
            } else {
                raw.into_pyobject(py).expect("str").into_any().unbind()
            }
        }
        "bool" => {
            let b = matches!(raw, "true" | "True" | "1" | "yes");
            pyo3::types::PyBool::new(py, b).to_owned().into_any().unbind()
        }
        _ => raw.into_pyobject(py).expect("str").into_any().unbind(),
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
