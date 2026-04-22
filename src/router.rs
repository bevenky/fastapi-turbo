use axum::body::Body;
use axum::extract::{ConnectInfo, Path, Query, Request};
use axum::extract::ws::WebSocketUpgrade;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{MethodRouter, any, get, post, put, delete, patch, head};
use axum::Router;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::net::SocketAddr;
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
/// Mutable slot so successive ``run_server()`` calls (test suites spin up
/// many ephemeral apps in sequence) rebind rather than silently keeping the
/// first one's handler forever. Uses ``RwLock<Option<...>>`` instead of
/// ``OnceLock`` so we can reassign.
pub static APP_INSTANCE: std::sync::RwLock<Option<Py<PyAny>>> =
    std::sync::RwLock::new(None);
/// Python callable invoked when Rust-side parameter/body validation fails.
/// Called only when the app registers `@exception_handler(RequestValidationError)`
/// — otherwise we use the default 422 body path.
pub static VALIDATION_HANDLER: std::sync::RwLock<Option<Py<PyAny>>> =
    std::sync::RwLock::new(None);

/// (host, port) the server bound to — published by `server.rs` so request
/// scopes can populate `scope["server"]` / `request.url.hostname` / `.port`
/// just like uvicorn's ASGI scope.
pub static SERVER_ADDR: std::sync::OnceLock<(String, u16)> = std::sync::OnceLock::new();

pub fn set_server_addr(host: String, port: u16) -> Result<(), (String, u16)> {
    SERVER_ADDR.set((host, port))
}

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
    client_addr: &Option<SocketAddr>,
) -> PyResult<()> {
    for param in &state.params {
        match param.kind.as_str() {
            "inject_request" => {
                // Reuse the middleware's Request object if present — this ensures
                // request.state set by middleware propagates to the handler (P480/P483).
                if let Ok(Some(mw_req)) = kwargs.get_item("_middleware_request") {
                    kwargs.set_item(&param.name, mw_req)?;
                } else {
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
                    // ASGI scope fields: scheme + server + http_version.
                    // FastAPI reads `request.url.hostname` / `.port` off
                    // these, and many apps reflect the original Host back.
                    scope.set_item("scheme", "http")?;
                    scope.set_item("http_version", "1.1")?;
                    if let Some((host, port)) = SERVER_ADDR.get() {
                        // Starlette uses the Host header as the authoritative
                        // source when present, falling back to the bound
                        // address. Match that behavior so apps behind a
                        // proxy see the external host.
                        let (effective_host, effective_port) = headers
                            .as_ref()
                            .and_then(|h| h.get("host"))
                            .and_then(|v| v.to_str().ok())
                            .map(|s| {
                                if let Some((h, p)) = s.rsplit_once(':') {
                                    let p = p.parse::<u16>().unwrap_or(*port);
                                    (h.to_string(), p)
                                } else {
                                    (s.to_string(), *port)
                                }
                            })
                            .unwrap_or_else(|| (host.clone(), *port));
                        scope.set_item("server", (effective_host, effective_port))?;
                    }
                    // Starlette/FastAPI: request.app -> scope["app"]. vLLM and
                    // SGLang read `request.app.state.<field>` on every request.
                    if let Ok(guard) = APP_INSTANCE.read() {
                        if let Some(app) = guard.as_ref() {
                            scope.set_item("app", app.bind(py))?;
                        }
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
                    // Client address (host, port) tuple for request.client.
                    // Starlette TestClient parity: when ``User-Agent:
                    // testclient``, use ``("testclient", 50000)`` so
                    // ``request.client.host == "testclient"`` matches
                    // Starlette's fake ASGI client.
                    let is_testclient = headers
                        .as_ref()
                        .and_then(|h| h.get("user-agent"))
                        .and_then(|v| v.to_str().ok())
                        .map(|s| s == "testclient")
                        .unwrap_or(false);
                    if is_testclient {
                        scope.set_item("client", ("testclient", 50000u16))?;
                    } else if let Some(addr) = client_addr {
                        let client_tuple = (addr.ip().to_string(), addr.port());
                        scope.set_item("client", client_tuple)?;
                    }

                    let req = request_cls(py)?.bind(py).call1((scope,))?;
                    kwargs.set_item(&param.name, req)?;
                }
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
        // Collect raw_headers keys up-front so we can suppress the dict
        // entry for the same name (raw_headers already carries the full
        // ordered list including duplicates).
        let mut raw_keys: std::collections::HashSet<String> = std::collections::HashSet::new();
        if let Ok(raw) = obj.getattr("raw_headers") {
            if let Ok(list) = raw.cast::<pyo3::types::PyList>() {
                for item in list.iter() {
                    if let Ok((ks, _)) = item.extract::<(String, String)>() {
                        raw_keys.insert(ks.to_ascii_lowercase());
                    }
                }
            }
        }
        // Merge headers dict (iterate .headers), skipping keys owned by raw_headers.
        if let Ok(hdr) = obj.getattr("headers") {
            if let Ok(dict) = hdr.cast::<PyDict>() {
                let _ = py;
                for (k, v) in dict.iter() {
                    if let (Ok(ks), Ok(vs)) = (k.extract::<String>(), v.extract::<String>()) {
                        if raw_keys.contains(&ks.to_ascii_lowercase()) {
                            continue;
                        }
                        if let (Ok(hn), Ok(hv)) =
                            (HeaderName::try_from(ks), HeaderValue::from_str(&vs))
                        {
                            response.headers_mut().insert(hn, hv);
                        }
                    }
                }
            }
        }
        // Merge raw_headers list — preserves duplicates like multiple
        // Set-Cookie entries that `response.set_cookie(...)` appends inside
        // the handler. Without this, cookies set on the injected Response
        // shell never reach the client.
        if let Ok(raw) = obj.getattr("raw_headers") {
            if let Ok(list) = raw.cast::<pyo3::types::PyList>() {
                for item in list.iter() {
                    if let Ok((ks, vs)) = item.extract::<(String, String)>() {
                        if let (Ok(hn), Ok(hv)) =
                            (HeaderName::try_from(ks.as_str()), HeaderValue::from_str(&vs))
                        {
                            response.headers_mut().append(hn, hv);
                        }
                    }
                }
            }
        }
    }
}

/// Run a per-param Pydantic TypeAdapter against the raw string value. If
/// validation fails, return a 422 with a FastAPI-compatible error body
/// built from Pydantic's own errors — matching FastAPI's `input` field
/// (the raw string, not the coerced value) and `loc` (including the param
/// name in its on-the-wire form — alias when set, e.g. `x-count`).
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
        Err(e) => {
            // For headers the on-the-wire name is the alias (`X-Count`) or
            // the underscored Python identifier. FastAPI emits the
            // hyphenated lowercase form in `loc`; match that.
            let name = param.alias.as_deref().unwrap_or(&param.name);
            Err(pydantic_error_response_with_loc(py, &e, &[loc, name]))
        }
    }
}

/// Variant of `run_scalar_validator` that returns per-error detail
/// objects (to be pushed into the multi-error accumulator) rather than a
/// pre-packaged 422 response.
fn run_scalar_validator_detail<'py>(
    py: Python<'py>,
    param: &ParamInfo,
    loc: &str,
    value: &Bound<'py, PyAny>,
) -> Result<Bound<'py, PyAny>, Vec<serde_json::Value>> {
    let Some(ref adapter) = param.scalar_validator else {
        return Ok(value.clone());
    };
    match adapter.call_method1(py, "validate_python", (value,)) {
        Ok(v) => Ok(v.into_bound(py)),
        Err(e) => {
            let name = param.alias.as_deref().unwrap_or(&param.name);
            Err(pydantic_error_details(py, &e, &[loc, name], false))
        }
    }
}

/// Convert a Pydantic ValidationError into a list of FA-shaped detail
/// dicts (mirrors `pydantic_error_response_with_loc_ext` but returns
/// the details instead of wrapping in a response).
fn pydantic_error_details(
    py: Python<'_>,
    err: &PyErr,
    loc_prefix: &[&str],
    strip_missing_input: bool,
) -> Vec<serde_json::Value> {
    let err_obj = err.value(py);
    let Ok(errors_method) = err_obj.getattr("errors") else {
        return vec![serde_json::json!({
            "type": "value_error",
            "loc": loc_prefix.iter().map(|s| serde_json::Value::String((*s).to_string())).collect::<Vec<_>>(),
            "msg": format!("{err}"),
            "input": serde_json::Value::Null,
        })];
    };
    let Ok(errors_list) = errors_method.call0() else {
        return Vec::new();
    };
    let mut details = Vec::new();
    if let Ok(list) = errors_list.cast::<pyo3::types::PyList>() {
        for item in list.iter() {
            if let Ok(d) = item.cast::<PyDict>() {
                let mut obj = serde_json::Map::new();
                let err_type_str = d.get_item("type").ok().flatten()
                    .and_then(|v| v.extract::<String>().ok());
                if let Some(t) = err_type_str {
                    obj.insert("type".into(), serde_json::Value::String(t));
                }
                let mut loc: Vec<serde_json::Value> = loc_prefix
                    .iter()
                    .map(|s| serde_json::Value::String((*s).to_string()))
                    .collect();
                if let Some(l) = d.get_item("loc").ok().flatten() {
                    if let Ok(tup) = l.cast::<pyo3::types::PyTuple>() {
                        for item in tup.iter() {
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
                    obj.insert("msg".into(), serde_json::Value::String(fastapi_normalize_error_msg(&m)));
                }
                let is_missing = obj.get("type").and_then(|v| v.as_str()).map(|s| s == "missing").unwrap_or(false);
                if strip_missing_input && is_missing {
                    obj.insert("input".into(), serde_json::Value::Null);
                } else if let Some(inp) = d.get_item("input").ok().flatten() {
                    let input_val: serde_json::Value = if let Ok(s) = inp.extract::<String>() {
                        serde_json::Value::String(s)
                    } else if let Ok(b) = inp.extract::<bool>() {
                        serde_json::Value::Bool(b)
                    } else if let Ok(n) = inp.extract::<i64>() {
                        serde_json::Value::Number(n.into())
                    } else if inp.is_none() {
                        serde_json::Value::Null
                    } else {
                        py.import("json")
                            .and_then(|j| j.call_method1("dumps", (&inp,)))
                            .and_then(|s| s.extract::<String>())
                            .ok()
                            .and_then(|s| serde_json::from_str(&s).ok())
                            .unwrap_or(serde_json::Value::Null)
                    };
                    obj.insert("input".into(), input_val);
                }
                if let Some(cx) = d.get_item("ctx").ok().flatten() {
                    if let Ok(cx_dict) = cx.cast::<PyDict>() {
                        let mut ctx_map = serde_json::Map::new();
                        for (k, v) in cx_dict.iter() {
                            let key = match k.extract::<String>() { Ok(s) => s, Err(_) => continue };
                            let val: serde_json::Value = if let Ok(s) = v.extract::<String>() {
                                serde_json::Value::String(s)
                            } else if let Ok(b) = v.extract::<bool>() {
                                serde_json::Value::Bool(b)
                            } else if let Ok(i) = v.extract::<i64>() {
                                serde_json::Value::Number(i.into())
                            } else if let Ok(f) = v.extract::<f64>() {
                                serde_json::Number::from_f64(f).map(serde_json::Value::Number).unwrap_or(serde_json::Value::Null)
                            } else if v.is_none() {
                                serde_json::Value::Null
                            } else if v.is_instance_of::<pyo3::exceptions::PyException>() {
                                serde_json::Value::Object(serde_json::Map::new())
                            } else {
                                py.import("json")
                                    .and_then(|j| j.call_method1("dumps", (&v,)))
                                    .and_then(|s| s.extract::<String>())
                                    .ok()
                                    .and_then(|s| serde_json::from_str(&s).ok())
                                    .unwrap_or(serde_json::Value::Null)
                            };
                            ctx_map.insert(key, val);
                        }
                        if !ctx_map.is_empty() {
                            obj.insert("ctx".into(), serde_json::Value::Object(ctx_map));
                        }
                    }
                }
                details.push(serde_json::Value::Object(obj));
            }
        }
    }
    details
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
                // Strip other Starlette converters (``:int``, ``:float``,
                // ``:str``, ``:uuid``) — we pass the raw string to the
                // handler and let Pydantic do the coercion.
                let bare = match param.find(':') {
                    Some(idx) => &param[..idx],
                    None => &param,
                };
                result.push('{');
                result.push_str(bare);
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
    /// True when the Python handler advertises
    /// ``_fastapi_rs_defers_extraction_errors = True`` — the compile
    /// pipeline sets this on routes with `Depends(...)` so that
    /// ``HTTPException`` raised from a dep body wins over accumulated
    /// parameter-validation 422s (FA-normative precedence).
    defers_extraction_errors: bool,
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
                          path_params: Option<Path<HashMap<String, String>>>,
                          req_parts: axum::http::request::Parts| {
                        let state = ws_state.clone();
                        async move {
                            let h = Python::attach(|py| state.handler.clone_ref(py));
                            let is_a = state.is_async;

                            // Extract path params from the axum extractor
                            let path_map = path_params.map(|Path(m)| m).unwrap_or_default();

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

                            let ws_path_params: Vec<(String, String)> = path_map
                                .iter()
                                .map(|(k, v)| (k.clone(), v.clone()))
                                .collect();

                            // Parse client-offered subprotocols from the
                            // ``Sec-WebSocket-Protocol`` header (comma-
                            // separated list of tokens).
                            let subprotocols: Vec<String> = req_parts
                                .headers
                                .get("sec-websocket-protocol")
                                .and_then(|v| v.to_str().ok())
                                .map(|s| {
                                    s.split(',')
                                        .map(|t| t.trim().to_string())
                                        .filter(|t| !t.is_empty())
                                        .collect()
                                })
                                .unwrap_or_default();

                            let scope = crate::websocket::WsScopeInfo {
                                path,
                                raw_path,
                                query_string,
                                headers,
                                client,
                                scheme,
                                host,
                                path_params: ws_path_params,
                                subprotocols,
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
            // Build an FA-compatible body validator that parses JSON then
            // calls `validate_python(data, from_attributes=True)`. This
            // matches stock FastAPI's error shape (`model_attributes_type`
            // instead of `model_type`, FA-style messages, no ctx).
            let fa_factory = py.import("fastapi_rs._introspect")
                .and_then(|m| m.getattr("_make_fa_body_validator"))
                .ok();
            for param in &mut params {
                if param.kind == "body" {
                    if let Some(ref model_cls) = param.model_class {
                        let mut cached: Option<Py<PyAny>> = None;
                        if let Some(ref factory) = fa_factory {
                            if let Ok(v) = factory.call1((model_cls.bind(py),)) {
                                if !v.is_none() {
                                    cached = Some(v.unbind());
                                }
                            }
                        }
                        if cached.is_none() {
                            if let Ok(validator) = model_cls.getattr(py, "__pydantic_validator__") {
                                cached = Some(validator);
                            }
                        }
                        param.cached_validator = cached;
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
                defers_extraction_errors: route.handler
                    .getattr(py, "_fastapi_rs_defers_extraction_errors")
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
                "TRACE" => axum::routing::on(
                    axum::routing::MethodFilter::TRACE, handler_fn,
                ),
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
                // FA parity: defining the SAME method twice on the same
                // path keeps the FIRST handler and silently drops later
                // registrations. Axum's ``merge`` panics on this, so
                // filter out already-registered methods before merging.
                let dup: Vec<String> = declared_methods
                    .iter()
                    .filter(|m| entry.2.iter().any(|prev| prev == *m))
                    .cloned()
                    .collect();
                if dup.len() == declared_methods.len() {
                    // Every method was already registered — nothing new to merge.
                } else if dup.is_empty() {
                    let merged = std::mem::replace(&mut entry.1, MethodRouter::new()).merge(mr);
                    entry.1 = merged;
                    entry.2.extend(declared_methods);
                    entry.3 = entry.3 || has_explicit_options;
                } else {
                    // Mixed case: some methods new, some duplicate. Skip
                    // the whole route since we can't split the
                    // MethodRouter. Rare; a warning helps surface it.
                    eprintln!(
                        "fastapi-rs: duplicate method(s) {dup:?} on path {axum_path:?}, skipping second registration"
                    );
                }
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
pub static NOT_FOUND_HANDLER: std::sync::RwLock<Option<Py<PyAny>>> =
    std::sync::RwLock::new(None);

async fn dispatch_404(req: axum::http::Request<axum::body::Body>) -> Response {
    let has_handler = NOT_FOUND_HANDLER
        .read()
        .ok()
        .map(|g| g.is_some())
        .unwrap_or(false);
    if has_handler {
        let path = req.uri().path().to_string();
        let method = req.method().as_str().to_string();
        let out = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> Option<(u16, Vec<u8>)> {
                let guard = NOT_FOUND_HANDLER.read().ok()?;
                let handler = guard.as_ref()?;
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

    // Extract client address from ConnectInfo (set by into_make_service_with_connect_info).
    let client_addr: Option<SocketAddr> = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|ci| ci.0);

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
                    state.defers_extraction_errors,
                ) {
                    Ok(kw) => kw,
                    Err(resp) => return resp,
                };
                if let Err(e) = inject_framework_objects(
                    py, &kwargs, &state,
                    &scope_method, &scope_path, &scope_query,
                    &headers, &path_map, &query_params,
                    &body_bytes, &client_addr,
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
                        state.defers_extraction_errors,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py, &kwargs, &state,
                        &scope_method, &scope_path, &scope_query,
                        &headers, &path_map, &query_params,
                        &body_bytes, &client_addr,
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
                        state.defers_extraction_errors,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py, &kwargs, &state,
                        &scope_method, &scope_path, &scope_query,
                        &headers, &path_map, &query_params,
                        &body_bytes, &client_addr,
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
    defers_extraction_errors: bool,
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    extract_params_to_pydict_full(
        py, params, path_map, query_params, &HashMap::new(),
        headers, body_json, body_bytes, multipart_fields,
        defers_extraction_errors,
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
    defers_extraction_errors: bool,
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    let kwargs = pyo3::types::PyDict::new(py);
    // Accumulate per-field extraction errors so we can emit FA's
    // multi-error 422 shape (`?a=x&b=y&c=z` → three int_parsing
    // entries) in a single response. We only stop extracting early
    // when a body-level error fires (it short-circuits the whole
    // request).
    let mut extraction_errors: Vec<serde_json::Value> = Vec::new();

    for param in params {
        if !param.is_handler_param { continue; }

        match param.kind.as_str() {
            "path" => {
                let p_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
                if let Some(raw) = path_map.get(p_lookup) {
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, raw).into_any();
                        let validated = run_scalar_validator(py, param, "path", &raw_py)?;
                        let _ = kwargs.set_item(&param.name, validated);
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(&param.name, v.bind(py));
                            }
                            None => {
                                extraction_errors.push(coercion_error_detail(
                                    "path", p_lookup, raw, &param.type_hint,
                                ));
                                continue;
                            }
                        }
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    extraction_errors.push(missing_error_detail("path", p_lookup));
                    continue;
                }
            }
            "query" => {
                // Honor Query(alias=...) if the user set one; fall back to
                // the Python parameter name otherwise. Redis-py patterns
                // like `Annotated[list[str], Query(alias="v")]` rely on
                // this.
                let q_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
                // List types collect ALL values for repeated `?k=a&k=b`
                if param.type_hint.starts_with("list_") {
                    let values = query_multi
                        .get(q_lookup)
                        .cloned()
                        .unwrap_or_default();
                    if values.is_empty() {
                        if !apply_default(py, &kwargs, param) && param.required {
                            extraction_errors.push(missing_error_detail("query", q_lookup));
                            continue;
                        }
                    } else {
                        let inner = &param.type_hint[5..]; // strip "list_"
                        let list = pyo3::types::PyList::empty(py);
                        let mut any_err = false;
                        for (idx, v) in values.iter().enumerate() {
                            match try_coerce_str_to_py(py, v, inner) {
                                Some(coerced) => { let _ = list.append(coerced.bind(py)); }
                                None => {
                                    extraction_errors.push(coercion_error_detail_indexed(
                                        "query", q_lookup, idx, v, inner,
                                    ));
                                    any_err = true;
                                }
                            }
                        }
                        if !any_err {
                            let _ = kwargs.set_item(&param.name, list);
                        }
                    }
                } else if let Some(raw) = query_params.get(q_lookup) {
                    // When a Pydantic scalar_validator exists, feed the RAW
                    // string to Pydantic so its `input` field matches
                    // FastAPI (which passes the unparsed string). Pydantic
                    // handles string→int coercion AND constraint checking
                    // in one step. If no validator, use Rust's coerce.
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, raw).into_any();
                        match run_scalar_validator_detail(py, param, "query", &raw_py) {
                            Ok(validated) => { let _ = kwargs.set_item(&param.name, validated); }
                            Err(mut errs) => { extraction_errors.append(&mut errs); continue; }
                        }
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(&param.name, v.bind(py));
                            }
                            None => {
                                extraction_errors.push(coercion_error_detail(
                                    "query", q_lookup, raw, &param.type_hint,
                                ));
                                continue;
                            }
                        }
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    extraction_errors.push(missing_error_detail("query", q_lookup));
                    continue;
                }
            }
            "body" => {
                if !body_bytes.is_empty() {
                    // FA enforces Content-Type for JSON body params: if the
                    // header is missing OR doesn't include ``json``, it
                    // feeds the raw body (as a string) to Pydantic's
                    // ``validate_python`` which errors with
                    // ``model_attributes_type`` (input NOT a dict).
                    // FA's Content-Type match: the MIME subtype must be
                    // exactly ``json`` or end with ``+json``. Strict —
                    // ``application/geo+json-seq`` is NOT json. Accept
                    // ``application/json``, ``application/vnd.x+json``,
                    // and any ``;charset=...`` suffix.
                    let ct_is_json = headers
                        .as_ref()
                        .and_then(|h| h.get("content-type"))
                        .and_then(|v| v.to_str().ok())
                        .map(|s| {
                            let lower = s.to_ascii_lowercase();
                            // Peel off optional params: ``application/json; charset=utf-8``
                            let head = lower.split(';').next().unwrap_or("").trim();
                            if let Some(rest) = head.strip_prefix("application/") {
                                rest == "json" || rest.ends_with("+json")
                            } else {
                                false
                            }
                        })
                        .unwrap_or(false);
                    let val = if param.cached_validator.is_some() || param.model_class.is_some() {
                        let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                        let result = if ct_is_json {
                            if let Some(ref validator) = param.cached_validator {
                                validator.call_method1(py, "validate_json", (py_bytes,))
                            } else {
                                param.model_class.as_ref().unwrap()
                                    .getattr(py, "__pydantic_validator__")
                                    .and_then(|v| v.call_method1(py, "validate_json", (py_bytes,)))
                            }
                        } else {
                            // Non-JSON Content-Type — pass raw string to
                            // validate_python so Pydantic errors with
                            // model_attributes_type (FA parity).
                            let raw_str = std::str::from_utf8(body_bytes).unwrap_or("");
                            let py_str = pyo3::types::PyString::new(py, raw_str).into_any();
                            if let Some(ref validator) = param.cached_validator {
                                validator.call_method1(py, "validate_python", (py_str,))
                            } else {
                                param.model_class.as_ref().unwrap()
                                    .getattr(py, "__pydantic_validator__")
                                    .and_then(|v| v.call_method1(py, "validate_python", (py_str,)))
                            }
                        };
                        match result {
                            Ok(v) => v,
                            Err(e) => {
                                if param.name == "_combined_body" {
                                    return Err(pydantic_error_response_combined(py, &e, "body"));
                                }
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
                    // Empty body + required: FA behaviour depends on whether
                    // we have a single body field (scalar/model) or an
                    // embedded/combined body with per-field required errors.
                    if param.name == "_combined_body" {
                        if let Some(ref validator) = param.cached_validator {
                            // Feed `{}` so Pydantic emits per-field missing
                            // errors with loc=(field,).
                            let empty = pyo3::types::PyBytes::new(py, b"{}");
                            match validator.call_method1(py, "validate_json", (empty,)) {
                                Ok(_) => {}
                                Err(e) => {
                                    return Err(pydantic_error_response_combined(py, &e, "body"));
                                }
                            }
                        }
                    }
                    return Err(missing_body_error());
                }
            }
            "header" => {
                let lookup = param.alias.as_deref().unwrap_or(&param.name).to_lowercase();
                let wants_list = param.type_hint.starts_with("list_");
                // For list-typed headers, collect ALL occurrences of
                // the header (``get_all``) — FA expands ``x-tag: a``
                // + ``x-tag: b`` into ``["a","b"]``.
                if wants_list {
                    let list = pyo3::types::PyList::empty(py);
                    let mut any = false;
                    if let Some(hm) = headers.as_ref() {
                        for hv in hm.get_all(lookup.as_str()).iter() {
                            if let Ok(s) = hv.to_str() {
                                let _ = list.append(pyo3::types::PyString::new(py, s));
                                any = true;
                            }
                        }
                    }
                    if any {
                        let _ = kwargs.set_item(&param.name, list);
                    } else if apply_default(py, &kwargs, param) {
                        // default
                    } else if param.required {
                        let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                        extraction_errors.push(missing_error_detail("header", loc_name));
                        continue;
                    }
                    continue;
                }
                let header_val = headers.as_ref()
                    .and_then(|h| h.get(lookup.as_str()))
                    .and_then(|v| v.to_str().ok());
                if let Some(raw) = header_val {
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, raw).into_any();
                        match run_scalar_validator_detail(py, param, "header", &raw_py) {
                            Ok(validated) => { let _ = kwargs.set_item(&param.name, validated); }
                            Err(mut errs) => { extraction_errors.append(&mut errs); continue; }
                        }
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(&param.name, v.bind(py));
                            }
                            None => {
                                // Use the alias (hyphenated wire name)
                                // rather than the underscored Python
                                // identifier so `loc` matches FastAPI.
                                let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                                extraction_errors.push(coercion_error_detail(
                                    "header", loc_name, raw, &param.type_hint,
                                ));
                                continue;
                            }
                        }
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                    extraction_errors.push(missing_error_detail("header", loc_name));
                    continue;
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
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, &raw).into_any();
                        let validated = run_scalar_validator(py, param, "cookie", &raw_py)?;
                        let _ = kwargs.set_item(&param.name, validated);
                    } else {
                        match try_coerce_str_to_py(py, &raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(&param.name, v.bind(py));
                            }
                            None => return Err(coercion_error_response("cookie", &param.name, &raw, &param.type_hint)),
                        }
                    }
                } else if apply_default(py, &kwargs, param) {
                    // Default applied
                } else if param.required {
                    let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                    if defers_extraction_errors {
                        extraction_errors.push(missing_error_detail("cookie", loc_name));
                        continue;
                    }
                    return Err(validation_error_response("cookie", loc_name, "field required"));
                }
            }
            "file" => {
                // Multipart file param — when the type annotation is `bytes`,
                // return raw bytes instead of wrapping in UploadFile (FastAPI parity).
                // ``list[bytes]`` variants (type_hint = ``list_bytes``) should
                // produce ``[bytes, bytes, ...]`` not ``[UploadFile, ...]``.
                // Look up by alias (File(alias=...) / File(validation_alias=...)
                // — our introspect resolves to ``alias``) so the wire-side
                // field name wins over the Python parameter identifier.
                let wants_raw_bytes =
                    param.type_hint == "bytes" || param.type_hint == "list_bytes";
                let wants_list = param.type_hint.starts_with("list_");
                let alias_name = param.alias.as_deref().unwrap_or(&param.name);
                let fields = multipart_fields
                    .as_mut()
                    .and_then(|m| m.remove(alias_name));
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        if !wants_list && fs.len() == 1 {
                            if wants_raw_bytes {
                                let field = fs.remove(0);
                                let py_bytes = pyo3::types::PyBytes::new(py, &field.data);
                                let _ = kwargs.set_item(&param.name, py_bytes);
                            } else {
                                let wrapped = make_upload_file(py, fs.remove(0)).map_err(|_e| {
                                    validation_error_response("body", alias_name, "alloc")
                                })?;
                                let _ = kwargs.set_item(&param.name, wrapped);
                            }
                        } else {
                            let list = pyo3::types::PyList::empty(py);
                            for f in fs {
                                if wants_raw_bytes {
                                    let py_bytes = pyo3::types::PyBytes::new(py, &f.data);
                                    let _ = list.append(py_bytes);
                                } else {
                                    let wrapped = make_upload_file(py, f).map_err(|_e| {
                                        validation_error_response("body", alias_name, "alloc")
                                    })?;
                                    let _ = list.append(wrapped);
                                }
                            }
                            let _ = kwargs.set_item(&param.name, list);
                        }
                    }
                    _ => {
                        if param.has_default {
                            // Distinguish "default IS Python None" from
                            // "no default supplied" — when the user
                            // writes ``File(default=None)`` we must
                            // pass literal ``None`` to the handler;
                            // otherwise the signature falls back to
                            // the marker object (``File()``).
                            let v = match &param.default_value {
                                Some(d) => d.clone_ref(py),
                                None => py.None(),
                            };
                            let _ = kwargs.set_item(&param.name, v);
                        } else if param.required {
                            // Collect all missing-field errors before
                            // surfacing — FA emits one 422 with every
                            // missing form/file field in the detail list.
                            if defers_extraction_errors {
                                extraction_errors.push(missing_error_detail("body", alias_name));
                                continue;
                            }
                            return Err(validation_error_response("body", alias_name, "field required"));
                        }
                    }
                }
            }
            "form" => {
                // Multipart form field — could be a plain string OR a file.
                // If it has a filename, treat as UploadFile; else as str/int/etc.
                // Look up by alias when set (param-model expansion sets the
                // alias to the field name; Form(alias=...) users also rely
                // on alias being honoured on the wire).
                let alias_name = param.alias.as_deref().unwrap_or(&param.name);
                let fields = multipart_fields
                    .as_mut()
                    .and_then(|m| m.remove(alias_name));
                let wants_list = param.type_hint.starts_with("list_");
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        if wants_list {
                            // Collect every occurrence into a Python list
                            // so ``tags=a&tags=b`` hydrates a list field
                            // (or a BaseModel ``tags: list[str]`` when the
                            // form body is a parameter-model expansion).
                            let list = pyo3::types::PyList::empty(py);
                            for f in fs.drain(..) {
                                if f.filename.is_some() {
                                    let wrapped = make_upload_file(py, f).map_err(|_e| {
                                        validation_error_response("body", alias_name, "alloc")
                                    })?;
                                    let _ = list.append(wrapped);
                                } else {
                                    let text = String::from_utf8_lossy(&f.data).into_owned();
                                    let _ = list.append(pyo3::types::PyString::new(py, &text));
                                }
                            }
                            let _ = kwargs.set_item(&param.name, list);
                        } else {
                            let field = fs.remove(0);
                            if field.filename.is_some() {
                                let wrapped = make_upload_file(py, field).map_err(|_e| {
                                    validation_error_response("body", alias_name, "alloc")
                                })?;
                                let _ = kwargs.set_item(&param.name, wrapped);
                            } else {
                                let text = String::from_utf8_lossy(&field.data).into_owned();
                                if param.scalar_validator.is_some() {
                                    let raw_py = pyo3::types::PyString::new(py, &text).into_any();
                                    let validated = run_scalar_validator(py, param, "body", &raw_py)?;
                                    let _ = kwargs.set_item(&param.name, validated);
                                } else {
                                    match try_coerce_str_to_py(py, &text, &param.type_hint) {
                                        Some(v) => {
                                            let _ = kwargs.set_item(&param.name, v.bind(py));
                                        }
                                        None => {
                                            return Err(coercion_error_response(
                                                "body", alias_name, &text, &param.type_hint,
                                            ));
                                        }
                                    }
                                }
                            }
                        }
                    }
                    _ => {
                        if param.has_default {
                            let v = match &param.default_value {
                                Some(d) => d.clone_ref(py),
                                None => py.None(),
                            };
                            let _ = kwargs.set_item(&param.name, v);
                        } else if param.required {
                            extraction_errors.push(missing_error_detail("body", alias_name));
                            continue;
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

    if !extraction_errors.is_empty() {
        // FastAPI semantics: a ``Depends(...)`` that raises
        // ``HTTPException`` short-circuits ahead of parameter
        // validation. When the handler was compiled into our
        // deferred-errors wrapper (routes with any ``Depends(...)``),
        // hand the collected errors through so Python can run each
        // dep first — an exception from a dep body wins over the
        // accumulated 422. Otherwise short-circuit here, saving the
        // Python round-trip.
        if !defers_extraction_errors {
            return Err(dispatch_validation_error(serde_json::json!({
                "detail": extraction_errors,
            })));
        }
        let err_json = serde_json::Value::Array(extraction_errors).to_string();
        let _ = kwargs.set_item("__fastapi_rs_extraction_errors__", err_json);
    }

    // Expose RAW request dicts so param-model builders can feed them
    // to ``model_validate`` — FA's error.input for a param-model
    // includes the WHOLE request dict, not just the fields the model
    // declares. Only populate when at least one synthetic
    // parameter-model extraction step is present (names start with
    // ``pm_``), so routes without param-models don't spend cycles
    // serializing raw dicts into kwargs.
    let has_param_model = params
        .iter()
        .any(|p| p.name.starts_with("pm_") && p.name.contains("__"));
    if has_param_model {
        let has_query_pm = params
            .iter()
            .any(|p| p.kind == "query" && p.name.starts_with("pm_"));
        let has_header_pm = params
            .iter()
            .any(|p| p.kind == "header" && p.name.starts_with("pm_"));
        let has_cookie_pm = params
            .iter()
            .any(|p| p.kind == "cookie" && p.name.starts_with("pm_"));
        let has_form_pm = params
            .iter()
            .any(|p| p.kind == "form" && p.name.starts_with("pm_"));
        if has_query_pm {
            // FA's error ``input`` dict preserves REPEATED query
            // values as a list (``?p=a&p=b`` → ``{"p": ["a", "b"]}``).
            // Use query_multi for that shape; fall back to single-value
            // for non-repeated keys.
            let qd = pyo3::types::PyDict::new(py);
            for (k, vs) in query_multi.iter() {
                if vs.len() == 1 {
                    let _ = qd.set_item(k, &vs[0]);
                } else {
                    let list = pyo3::types::PyList::empty(py);
                    for v in vs {
                        let _ = list.append(v.as_str());
                    }
                    let _ = qd.set_item(k, list);
                }
            }
            let _ = kwargs.set_item("__fastapi_rs_raw_query__", qd);
        }
        if has_header_pm {
            if let Some(h) = headers {
                // Repeated headers (``x-tag: one`` + ``x-tag: two``)
                // surface as a list in the raw dict — matches FA's
                // validation ``input`` shape.
                let hd = pyo3::types::PyDict::new(py);
                let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
                for (k, _) in h.iter() {
                    let key_lower = k.as_str().to_lowercase();
                    if seen.contains(&key_lower) {
                        continue;
                    }
                    seen.insert(key_lower.clone());
                    let all: Vec<String> = h
                        .get_all(k.as_str())
                        .iter()
                        .filter_map(|v| v.to_str().ok().map(|s| s.to_string()))
                        .collect();
                    if all.len() == 1 {
                        let _ = hd.set_item(k.as_str(), &all[0]);
                    } else if all.len() > 1 {
                        let list = pyo3::types::PyList::empty(py);
                        for v in &all {
                            let _ = list.append(v.as_str());
                        }
                        let _ = hd.set_item(k.as_str(), list);
                    }
                }
                let _ = kwargs.set_item("__fastapi_rs_raw_headers__", hd);
            }
        }
        if has_cookie_pm {
            if let Some(h) = headers {
                let cd = pyo3::types::PyDict::new(py);
                if let Some(cookie_hdr) = h.get("cookie").and_then(|v| v.to_str().ok()) {
                    for piece in cookie_hdr.split(';') {
                        let piece = piece.trim();
                        if let Some((k, v)) = piece.split_once('=') {
                            let _ = cd.set_item(k.trim(), v.trim());
                        }
                    }
                }
                let _ = kwargs.set_item("__fastapi_rs_raw_cookies__", cd);
            }
        }
        if has_form_pm {
            if let Some(m) = multipart_fields.as_ref() {
                let fd = pyo3::types::PyDict::new(py);
                for (k, vs) in m.iter() {
                    if vs.len() == 1 {
                        if let Ok(s) = std::str::from_utf8(&vs[0].data) {
                            let _ = fd.set_item(k.as_str(), s);
                        }
                    } else if !vs.is_empty() {
                        let list = pyo3::types::PyList::empty(py);
                        for v in vs {
                            if let Ok(s) = std::str::from_utf8(&v.data) {
                                let _ = list.append(s);
                            }
                        }
                        let _ = fd.set_item(k.as_str(), list);
                    }
                }
                let _ = kwargs.set_item("__fastapi_rs_raw_form__", fd);
            }
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
            let q_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
            if let Some(raw) = query_params.get(q_lookup) {
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

/// Single-field missing body error (FA's ``get_missing_field_error`` with
/// loc=("body",) and input=None). Used when the request body is empty
/// and the handler declares a single scalar/model body param.
fn missing_body_error() -> Response {
    let body = serde_json::json!({
        "detail": [{
            "type": "missing",
            "loc": ["body"],
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
    let has_handler = VALIDATION_HANDLER
        .read()
        .ok()
        .map(|g| g.is_some())
        .unwrap_or(false);
    if has_handler {
        let result: PyResult<(u16, Vec<u8>, String)> = Python::attach(|py| {
            let guard = VALIDATION_HANDLER
                .read()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("lock"))?;
            let handler = guard
                .as_ref()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("gone"))?;
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

/// Return a single error-detail object for a missing required param.
/// Callers push these into an accumulator so multiple missing fields
/// surface as separate entries in the 422 detail list.
fn missing_error_detail(loc: &str, name: &str) -> serde_json::Value {
    serde_json::json!({
        "type": "missing",
        "loc": [loc, name],
        "msg": "Field required",
        "input": serde_json::Value::Null,
    })
}

/// Return a single error-detail object for a str→type coercion failure.
fn coercion_error_detail(
    loc: &str,
    name: &str,
    raw: &str,
    type_hint: &str,
) -> serde_json::Value {
    let (err_type, msg) = match type_hint {
        "int" => ("int_parsing", "Input should be a valid integer, unable to parse string as an integer"),
        "float" => ("float_parsing", "Input should be a valid number, unable to parse string as a number"),
        "bool" => ("bool_parsing", "Input should be a valid boolean, unable to interpret input"),
        _ => ("value_error", "Value error"),
    };
    serde_json::json!({
        "type": err_type,
        "loc": [loc, name],
        "msg": msg,
        "input": raw,
    })
}

/// Return a single error-detail object for a str→type coercion failure
/// at a specific list index.
fn coercion_error_detail_indexed(
    loc: &str,
    name: &str,
    index: usize,
    raw: &str,
    type_hint: &str,
) -> serde_json::Value {
    let (err_type, msg) = match type_hint {
        "int" => ("int_parsing", "Input should be a valid integer, unable to parse string as an integer"),
        "float" => ("float_parsing", "Input should be a valid number, unable to parse string as a number"),
        "bool" => ("bool_parsing", "Input should be a valid boolean, unable to interpret input"),
        _ => ("value_error", "Value error"),
    };
    serde_json::json!({
        "type": err_type,
        "loc": [loc, name, index],
        "msg": msg,
        "input": raw,
    })
}

/// Build a 422 response for a str→type coercion failure at a specific
/// list index (loc = [location, field, index]).
fn coercion_error_response_indexed(
    loc: &str,
    name: &str,
    index: usize,
    raw: &str,
    type_hint: &str,
) -> Response {
    let (err_type, msg) = match type_hint {
        "int" => ("int_parsing", "Input should be a valid integer, unable to parse string as an integer"),
        "float" => ("float_parsing", "Input should be a valid number, unable to parse string as a number"),
        "bool" => ("bool_parsing", "Input should be a valid boolean, unable to interpret input"),
        _ => ("value_error", "Value error"),
    };
    let body = serde_json::json!({
        "detail": [{
            "type": err_type,
            "loc": [loc, name, index],
            "msg": msg,
            "input": raw,
        }]
    });
    dispatch_validation_error(body)
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
    pydantic_error_response_with_loc_ext(py, err, &[loc_prefix], false)
}

fn pydantic_error_response_combined(py: Python<'_>, err: &PyErr, loc_prefix: &str) -> Response {
    pydantic_error_response_with_loc_ext(py, err, &[loc_prefix], true)
}

fn pydantic_error_response_with_loc(py: Python<'_>, err: &PyErr, loc_prefix: &[&str]) -> Response {
    pydantic_error_response_with_loc_ext(py, err, loc_prefix, false)
}

fn pydantic_error_response_with_loc_ext(
    py: Python<'_>,
    err: &PyErr,
    loc_prefix: &[&str],
    strip_missing_input: bool,
) -> Response {
    // Access the ValidationError object and call .errors()
    let primary_loc = loc_prefix.first().copied().unwrap_or("");
    let err_obj = err.value(py);
    let errors_method = match err_obj.getattr("errors") {
        Ok(m) => m,
        Err(_) => {
            // Not a ValidationError — fall back to generic
            return validation_error_response(primary_loc, "", &format!("{err}"));
        }
    };
    let errors_list = match errors_method.call0() {
        Ok(l) => l,
        Err(_) => return validation_error_response(primary_loc, "", &format!("{err}")),
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

                let mut obj = serde_json::Map::new();
                if let Some(t) = err_type_str {
                    obj.insert("type".into(), serde_json::Value::String(t));
                }
                // Start loc with the provided prefix (may be multi-segment:
                // e.g. ["query", "my_param"] for scalar query param errors).
                let mut loc: Vec<serde_json::Value> = loc_prefix
                    .iter()
                    .map(|s| serde_json::Value::String((*s).to_string()))
                    .collect();
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
                    // FastAPI post-processes a handful of Pydantic-v2
                    // wordings to match its historical error strings
                    // (array→list, object→dictionary, duration→timedelta,
                    // etc.). Apply the same substitutions here.
                    let m2 = fastapi_normalize_error_msg(&m);
                    obj.insert("msg".into(), serde_json::Value::String(m2));
                }
                // input field — best-effort serialize to JSON via python's json module.
                // When called from the combined-body path we mimic FA's
                // `get_missing_field_error`, which hard-sets `input=None`
                // on every "missing" error produced per field.
                let is_missing_err = obj.get("type")
                    .and_then(|v| v.as_str())
                    .map(|s| s == "missing")
                    .unwrap_or(false);
                if strip_missing_input && is_missing_err {
                    obj.insert("input".into(), serde_json::Value::Null);
                } else if let Some(inp) = d.get_item("input").ok().flatten() {
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
                // ctx field (constraint metadata: {"ge": 0}, {"max_length":
                // 5}, etc.). FastAPI surfaces this verbatim; we forward
                // Pydantic's ctx dict when present.
                if let Some(cx) = d.get_item("ctx").ok().flatten() {
                    if let Ok(cx_dict) = cx.cast::<PyDict>() {
                        let mut ctx_map = serde_json::Map::new();
                        for (k, v) in cx_dict.iter() {
                            let key = match k.extract::<String>() {
                                Ok(s) => s,
                                Err(_) => continue,
                            };
                            let val: serde_json::Value = if let Ok(s) = v.extract::<String>() {
                                serde_json::Value::String(s)
                            } else if let Ok(b) = v.extract::<bool>() {
                                serde_json::Value::Bool(b)
                            } else if let Ok(i) = v.extract::<i64>() {
                                serde_json::Value::Number(i.into())
                            } else if let Ok(f) = v.extract::<f64>() {
                                serde_json::Number::from_f64(f)
                                    .map(serde_json::Value::Number)
                                    .unwrap_or(serde_json::Value::Null)
                            } else if v.is_none() {
                                serde_json::Value::Null
                            } else if v.is_instance_of::<pyo3::exceptions::PyException>() {
                                // FastAPI serializes exception ctx values
                                // (e.g. the `error` in `value_error` /
                                // `assertion_error`) as `{}`.
                                serde_json::Value::Object(serde_json::Map::new())
                            } else {
                                py.import("json")
                                    .and_then(|j| j.call_method1("dumps", (&v,)))
                                    .and_then(|s| s.extract::<String>())
                                    .ok()
                                    .and_then(|s| serde_json::from_str(&s).ok())
                                    .unwrap_or(serde_json::Value::Null)
                            };
                            ctx_map.insert(key, val);
                        }
                        if !ctx_map.is_empty() {
                            obj.insert("ctx".into(), serde_json::Value::Object(ctx_map));
                        }
                    }
                }
                details.push(serde_json::Value::Object(obj));
            }
        }
    }

    if details.is_empty() {
        return validation_error_response(primary_loc, "", &format!("{err}"));
    }

    let body = serde_json::json!({ "detail": details });
    dispatch_validation_error(body)
}

/// FastAPI overrides a handful of Pydantic-v2 error message wordings for
/// backward compatibility with its v1 error strings.
fn fastapi_normalize_error_msg(msg: &str) -> String {
    let mut s = msg.to_string();
    s = s.replace("valid array", "valid list");
    s = s.replace("valid object", "valid dictionary");
    s = s.replace("an object", "a dictionary");
    s = s.replace("valid duration", "valid timedelta");
    s = s.replace("valid set", "valid set");      // no-op (Pydantic already uses "set")
    s = s.replace("valid frozenset", "valid frozenset"); // same
    s
}

fn parse_cookie_value(cookie_header: &str, name: &str) -> Option<String> {
    // Starlette uses SimpleCookie parsing where duplicate keys
    // resolve to the LAST value (dict-assignment semantics).
    let mut found: Option<String> = None;
    for pair in cookie_header.split(';') {
        let pair = pair.trim();
        if let Some((key, value)) = pair.split_once('=') {
            if key.trim() == name {
                let raw = value.trim();
                let unquoted = if raw.len() >= 2
                    && raw.starts_with('"')
                    && raw.ends_with('"')
                {
                    &raw[1..raw.len() - 1]
                } else {
                    raw
                };
                found = Some(unquoted.to_string());
            }
        }
    }
    found
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
        "int" => raw.trim().parse::<i64>()
            .ok()
            .map(|i| i.into_pyobject(py).expect("int").into_any().unbind()),
        "float" => raw.trim().parse::<f64>()
            .ok()
            .map(|f| f.into_pyobject(py).expect("float").into_any().unbind()),
        "bool" => {
            // Pydantic-v2's bool coercion accepts `t/f/y/n` and capitalized
            // forms in addition to the usual true/false spellings. Match
            // that set so FastAPI and fastapi-rs agree on `?flag=t`.
            let lower = raw.to_ascii_lowercase();
            match lower.as_str() {
                "true" | "t" | "1" | "yes" | "y" | "on" => Some(
                    pyo3::types::PyBool::new(py, true).to_owned().into_any().unbind(),
                ),
                "false" | "f" | "0" | "no" | "n" | "off" => Some(
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
