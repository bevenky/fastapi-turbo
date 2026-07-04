use axum::body::Body;
use axum::extract::ws::WebSocketUpgrade;
use axum::extract::{ConnectInfo, Path, Query, Request};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, delete, get, head, patch, post, put, MethodRouter};
use axum::Router;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

use crate::multipart::{parse_boundary, parse_multipart, ParsedField, PyUploadFile};
use crate::responses::{py_to_response_with_request, pyerr_to_response, serde_to_pyobj};
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
pub static APP_INSTANCE: std::sync::RwLock<Option<Py<PyAny>>> = std::sync::RwLock::new(None);
/// Python callable invoked when Rust-side parameter/body validation fails.
/// Called only when the app registers `@exception_handler(RequestValidationError)`
/// — otherwise we use the default 422 body path.
pub static VALIDATION_HANDLER: std::sync::RwLock<Option<Py<PyAny>>> = std::sync::RwLock::new(None);

/// (host, port) the server bound to — published by `server.rs` so request
/// scopes can populate `scope["server"]` / `request.url.hostname` / `.port`
/// just like uvicorn's ASGI scope.
pub static SERVER_ADDR: std::sync::OnceLock<(String, u16)> = std::sync::OnceLock::new();

pub fn set_server_addr(host: String, port: u16) -> Result<(), (String, u16)> {
    SERVER_ADDR.set((host, port))
}

fn bg_tasks_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = BG_TASKS_CLS.get() {
        return Ok(c);
    }
    let cls: Py<PyAny> = py
        .import("fastapi_turbo.background")?
        .getattr("BackgroundTasks")?
        .unbind();
    let _ = BG_TASKS_CLS.set(cls);
    Ok(BG_TASKS_CLS.get().unwrap())
}

fn request_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = REQUEST_CLS.get() {
        return Ok(c);
    }
    // Door factory (not the class): enriches the minimal Rust-built scope with
    // the keys a real Starlette Request needs (root_path, state, request-line
    // defaults) and returns a Request. Called the same way — call1((scope,)).
    let cls: Py<PyAny> = py
        .import("fastapi_turbo.requests")?
        .getattr("_door_make_request")?
        .unbind();
    let _ = REQUEST_CLS.set(cls);
    Ok(REQUEST_CLS.get().unwrap())
}

/// In-process disconnect flag for the streaming door: carried on the request's
/// Axum extensions from `process_request_streaming` through the router to
/// `handle_request`, then stashed in the Python ``Request`` scope so
/// ``request.is_disconnected()`` can observe a client drop (the door's
/// receive-poller sets it). The inner object is a Python ``threading.Event``.
/// ``Arc`` so it satisfies the ``Clone + Send + Sync`` extension bound.
#[derive(Clone)]
pub struct DisconnectFlag(pub std::sync::Arc<Py<PyAny>>);

thread_local! {
    /// Per-request disconnect flag, set by `handle_request` (from the extension)
    /// and read where the Python ``Request`` scope is built. Thread-local because
    /// `handle_request` and `build_injected_object` run on the same worker thread
    /// (the latter inside `block_in_place`).
    static REQUEST_DISCONNECT_FLAG: std::cell::RefCell<Option<Py<PyAny>>> =
        const { std::cell::RefCell::new(None) };

    /// Per-request SHARED injected ``Response``. FastAPI gives the handler AND
    /// every dependency that takes ``response: Response`` the SAME object, so a
    /// dep can set ``response.headers[...]`` / ``status_code`` and it carries onto
    /// the final response. ``build_injected_object`` builds it lazily (the first
    /// ``inject_response`` need wins, all later ones clone the same ref) and
    /// ``apply_injected_response`` merges it onto the outgoing response — including
    /// the case where ONLY a dependency (not the handler) injected it, which the
    /// old kwargs-only merge missed. Thread-local + reset per request, same
    /// worker-thread invariant as the disconnect flag.
    static INJECTED_RESPONSE: std::cell::RefCell<Option<Py<PyAny>>> =
        const { std::cell::RefCell::new(None) };

    /// Per-request SHARED injected ``BackgroundTasks``. FastAPI gives the handler
    /// AND every dependency that takes ``background_tasks: BackgroundTasks`` the
    /// SAME instance, so a dep can add tasks and they run after the response.
    /// ``build_injected_object`` builds it lazily (first need wins, later ones
    /// share); ``drain_background_tasks`` drains this one instance. Reset per
    /// request (worker-thread invariant, like INJECTED_RESPONSE).
    static INJECTED_BACKGROUND_TASKS: std::cell::RefCell<Option<Py<PyAny>>> =
        const { std::cell::RefCell::new(None) };

    /// Per-request SHARED injected ``Request``. FastAPI hands the handler AND
    /// every dependency that takes ``request: Request`` the SAME object, so a
    /// dep's ``request.state`` writes (auth context, traces) are visible to
    /// later deps and the handler. ``build_injected_object`` builds it lazily
    /// (first need wins — the dep-resolution loop runs before handler-kwarg
    /// injection, so deps and handler share one instance). Reset per request
    /// (worker-thread invariant, like INJECTED_RESPONSE); the async-inline
    /// path takes it off the loop thread's TL alongside the other shells.
    static INJECTED_REQUEST: std::cell::RefCell<Option<Py<PyAny>>> =
        const { std::cell::RefCell::new(None) };

    /// Per-request route-level default status code (``@app.get(status_code=201)``).
    /// Set by handle_request from RouteState; read by ``py_to_response`` to status
    /// non-Response handler results. Reset per request (worker-thread invariant).
    static ROUTE_DEFAULT_STATUS: std::cell::Cell<Option<u16>> = const { std::cell::Cell::new(None) };

    /// The app served by THIS worker thread's runtime. ``run_server`` sets it once
    /// per worker thread (``on_thread_start``) so the door's error-capture sites
    /// append a 500 to the RIGHT app's ``_captured_server_exceptions`` even when
    /// several apps run their own loopback servers in one process (parametrized
    /// tests). Falls back to the global ``APP_INSTANCE`` when unset (in-process
    /// door / single app). Not reset per request — it's a per-thread/per-server
    /// binding, not per-request state.
    static CURRENT_APP: std::cell::RefCell<Option<Py<PyAny>>> =
        const { std::cell::RefCell::new(None) };
}

/// Bind the current worker thread to its server's app (called from
/// ``run_server``'s ``on_thread_start``).
pub(crate) fn set_current_app(app: Option<Py<PyAny>>) {
    CURRENT_APP.with(|c| *c.borrow_mut() = app);
}

/// The app handling the current request: the per-thread ``CURRENT_APP`` if the
/// thread was bound by ``run_server``, else the global ``APP_INSTANCE`` (in-process
/// door / single-app ``app.run``). Used by the 500-capture / dep-exception sites.
pub(crate) fn current_app(py: Python<'_>) -> Option<Py<PyAny>> {
    CURRENT_APP
        .with(|c| c.borrow().as_ref().map(|a| a.clone_ref(py)))
        .or_else(|| {
            APP_INSTANCE
                .read()
                .ok()
                .and_then(|g| g.as_ref().map(|a| a.clone_ref(py)))
        })
}

/// The route-level default status for the request currently being served on this
/// worker thread (``None`` → 200). Read by ``crate::responses::py_to_response``
/// so a ``status_code=201`` route statuses its dict/list/None result.
pub(crate) fn route_default_status() -> axum::http::StatusCode {
    ROUTE_DEFAULT_STATUS
        .with(|s| s.get())
        .and_then(|c| axum::http::StatusCode::from_u16(c).ok())
        .unwrap_or(axum::http::StatusCode::OK)
}

/// True when an injected Response shell exists for the current request — i.e. a
/// handler/dep may still override the status. ``py_to_response`` uses this to AVOID
/// pre-stripping a no-body body on the route default (e.g. ``status_code=204``)
/// when the handler might override to a body status (``response.status_code=400``);
/// shell routes are stripped post-merge in ``apply_injected_response`` on the final
/// status instead.
pub(crate) fn has_injected_response() -> bool {
    INJECTED_RESPONSE.with(|c| c.borrow().is_some())
}

/// RAII guard: clears the per-request thread-locals on drop so they never leak to
/// the next request served on the same worker thread.
struct DisconnectFlagGuard;
impl Drop for DisconnectFlagGuard {
    fn drop(&mut self) {
        REQUEST_DISCONNECT_FLAG.with(|f| {
            *f.borrow_mut() = None;
        });
        INJECTED_RESPONSE.with(|r| {
            *r.borrow_mut() = None;
        });
        INJECTED_BACKGROUND_TASKS.with(|b| {
            *b.borrow_mut() = None;
        });
        INJECTED_REQUEST.with(|r| {
            *r.borrow_mut() = None;
        });
        ROUTE_DEFAULT_STATUS.with(|s| s.set(None));
    }
}

fn response_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    if let Some(c) = RESPONSE_CLS.get() {
        return Ok(c);
    }
    let cls: Py<PyAny> = py
        .import("fastapi_turbo.responses")?
        .getattr("Response")?
        .unbind();
    let _ = RESPONSE_CLS.set(cls);
    Ok(RESPONSE_CLS.get().unwrap())
}

/// Cached handle to ``fastapi_turbo.applications._set_current_request_scope``.
/// Populates the request-scoped ContextVar so ``@app.exception_handler``
/// callbacks can see the real path/method/query via ``request.url.path``
/// even when the handler itself doesn't declare ``request: Request``.
static SET_REQUEST_SCOPE: std::sync::OnceLock<pyo3::Py<pyo3::PyAny>> = std::sync::OnceLock::new();

fn set_request_scope_ctxvar(
    py: pyo3::Python<'_>,
    method: &Option<String>,
    path: &Option<String>,
    query: &Option<String>,
    state: &RouteState,
) {
    let func = SET_REQUEST_SCOPE.get_or_init(|| {
        py.import("fastapi_turbo.applications")
            .and_then(|m| m.getattr("_set_current_request_scope"))
            .map(|f| f.unbind())
            .unwrap_or_else(|_| py.None())
    });
    if func.is_none(py) {
        return;
    }

    // Zero-consumer fast path: when nothing reads the request-scope ctxvar for
    // this app (no user exception handlers, no Sentry), skip the endpoint/route
    // getattrs, the kwargs dict build, and the Python call entirely. This is the
    // bench/common handler-only case and removes ~4-5μs from every request.
    if !state.wants_request_scope {
        return;
    }

    // Pull the user endpoint and route path off the RouteState for
    // Sentry-style transaction refinement. The handler is a compiled
    // wrapper; the original endpoint lives on its
    // ``_fastapi_turbo_original_endpoint`` attribute (set at compile).
    // Fall back to the wrapper itself if the original wasn't stashed.
    let endpoint_obj = state
        .handler
        .bind(py)
        .getattr("_fastapi_turbo_original_endpoint")
        .unwrap_or_else(|_| state.handler.bind(py).clone());
    let route_path_opt: Option<String> = state
        .route_obj
        .as_ref()
        .and_then(|r| r.bind(py).getattr("path").ok())
        .and_then(|p| p.extract::<String>().ok());

    let m = method.as_deref().map(|s| pyo3::types::PyString::new(py, s));
    let p = path.as_deref().map(|s| pyo3::types::PyString::new(py, s));
    let q = query.as_deref().map(|s| pyo3::types::PyString::new(py, s));
    let rp = route_path_opt
        .as_deref()
        .map(|s| pyo3::types::PyString::new(py, s));

    let kwargs = pyo3::types::PyDict::new(py);
    let _ = kwargs.set_item("endpoint", endpoint_obj);
    if let Some(rp_str) = rp {
        let _ = kwargs.set_item("route_path", rp_str);
    }

    let args = (
        m.map(|b| b.into_any())
            .unwrap_or_else(|| py.None().into_bound(py)),
        p.map(|b| b.into_any())
            .unwrap_or_else(|| py.None().into_bound(py)),
        q.map(|b| b.into_any())
            .unwrap_or_else(|| py.None().into_bound(py)),
    );
    let _ = func.bind(py).call(args, Some(&kwargs));
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

/// Build one framework-provided object for an ``inject_*`` kind (Request /
/// BackgroundTasks / Response / SecurityScopes). Shared by handler-kwarg
/// injection and dependency-input resolution (a dep that takes ``request:
/// Request`` etc.), so both produce the same object shape.
#[allow(clippy::too_many_arguments)]
fn build_injected_object(
    py: Python<'_>,
    kind: &str,
    state: &RouteState,
    scope_method: &Option<String>,
    scope_path: &Option<String>,
    scope_query: &Option<String>,
    headers: &Option<HeaderMap>,
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    body_bytes: &[u8],
    client_addr: &Option<SocketAddr>,
    oauth_scopes: &[String],
) -> PyResult<Py<PyAny>> {
    match kind {
        "inject_request" => {
            // Per-request SHARED Request (FastAPI semantics): the first need
            // builds it, every later one — a second dep, the handler kwarg —
            // reuses the same object so ``request.state`` writes relay across
            // deps and into the handler.
            if let Some(existing) =
                INJECTED_REQUEST.with(|cell| cell.borrow().as_ref().map(|o| o.clone_ref(py)))
            {
                return Ok(existing);
            }
            // Build an ASGI-ish scope dict
            let scope = PyDict::new(py);
            scope.set_item("type", "http")?;
            scope.set_item("method", scope_method.as_deref().unwrap_or("GET"))?;
            scope.set_item("path", scope_path.as_deref().unwrap_or("/"))?;
            let qs_bytes: &[u8] = scope_query.as_deref().map(|s| s.as_bytes()).unwrap_or(b"");
            scope.set_item("query_string", pyo3::types::PyBytes::new(py, qs_bytes))?;
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
                scope.set_item("_body", pyo3::types::PyBytes::new(py, body_bytes))?;
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
            // Starlette/FA: ``request.scope["route"]`` exposes
            // the matched APIRoute. Some handlers read it to
            // pull route metadata (path template, methods).
            if let Some(ref route) = state.route_obj {
                scope.set_item("route", route.bind(py))?;
            }
            // In-process disconnect flag (streaming door): expose it on the scope
            // so ``request.is_disconnected()`` can observe a client drop. Only
            // present for apps with is_disconnected endpoints (the door sets it).
            REQUEST_DISCONNECT_FLAG.with(|f| {
                if let Some(flag) = f.borrow().as_ref() {
                    let _ = scope.set_item("_fastapi_turbo_disconnect", flag.bind(py));
                }
            });

            let req = request_cls(py)?.bind(py).call1((scope,))?.unbind();
            INJECTED_REQUEST.with(|cell| *cell.borrow_mut() = Some(req.clone_ref(py)));
            Ok(req)
        }
        "inject_background_tasks" => INJECTED_BACKGROUND_TASKS.with(|cell| {
            // Return the per-request SHARED BackgroundTasks (FastAPI semantics) so
            // the handler AND every dependency add to the SAME instance — which
            // ``drain_background_tasks`` then runs after the response. (A dep's
            // tasks were previously dropped: it got its own instance, in ``resolved``,
            // never drained.)
            let mut slot = cell.borrow_mut();
            if let Some(existing) = slot.as_ref() {
                return Ok(existing.clone_ref(py));
            }
            let bg = bg_tasks_cls(py)?.bind(py).call0()?;
            // Stash the current app on the BackgroundTasks instance
            // so ``run_sync`` can pass ``app=`` when submitting async
            // tasks to the worker loop — preserves per-app timeout
            // isolation for work that runs *after* the response.
            if let Ok(app_slot) = APP_INSTANCE.read() {
                if let Some(app) = app_slot.as_ref() {
                    let _ = bg.setattr("_app", app.bind(py));
                }
            }
            let bg = bg.unbind();
            *slot = Some(bg.clone_ref(py));
            Ok(bg)
        }),
        "inject_response" => INJECTED_RESPONSE.with(|cell| {
            // Return the per-request SHARED Response (FastAPI semantics) so the
            // handler and every dependency mutate the same object.
            let mut slot = cell.borrow_mut();
            if let Some(existing) = slot.as_ref() {
                Ok(existing.clone_ref(py))
            } else {
                let resp = response_cls(py)?.bind(py).call0()?;
                // Mimic FastAPI's solve_dependencies: this Response is a SHELL for
                // status/headers only. Drop its empty-body ``content-length`` and
                // null its ``status_code`` so merging it (apply_injected_response)
                // never clobbers the real response's body length or status when the
                // user didn't set them.
                if let Ok(headers) = resp.getattr("headers") {
                    let _ = headers.call_method1("__delitem__", ("content-length",));
                }
                let _ = resp.setattr("status_code", py.None());
                let resp = resp.unbind();
                *slot = Some(resp.clone_ref(py));
                Ok(resp)
            }
        }),
        "inject_security_scopes" => {
            // ``SecurityScopes(scopes=...)`` — the adapter accumulates the scopes
            // declared down the ``Security(..., scopes=[...])`` chain into the
            // param's ``oauth_scopes`` at build time (FastAPI's
            // ``dependant.security_scopes``). ``fastapi_turbo.security
            // .SecurityScopes`` is the real ``fastapi.security`` class.
            let ss_mod = py.import("fastapi_turbo.security")?;
            let ss_cls = ss_mod.getattr("SecurityScopes")?;
            let scopes_list = pyo3::types::PyList::empty(py);
            for s in oauth_scopes {
                scopes_list.append(s)?;
            }
            let kw = PyDict::new(py);
            kw.set_item("scopes", scopes_list)?;
            Ok(ss_cls.call((), Some(&kw))?.unbind())
        }
        _ => Ok(py.None()),
    }
}

/// Inject framework-provided objects (Request / BackgroundTasks / Response)
/// as handler kwargs right before dispatch. Handlers ask for them by type.
/// Dependency-input inject params (``is_handler_param == false``) are built into
/// the resolver's `resolved` map instead, so they're skipped here.
#[allow(clippy::too_many_arguments)]
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
    // Precomputed at startup: no inject_* param anywhere on the route →
    // skip the per-param kind scan entirely (the common body/path-only case).
    if !state.has_inject_any {
        return Ok(());
    }
    for param in &state.params {
        if !param.is_handler_param {
            continue;
        }
        match param.kind.as_str() {
            "inject_request" => {
                // Reuse the middleware's Request object if present — this ensures
                // request.state set by middleware propagates to the handler (P480/P483).
                if let Ok(Some(mw_req)) = kwargs.get_item("_middleware_request") {
                    kwargs.set_item(param.name_pystr(py), mw_req)?;
                } else {
                    let req = build_injected_object(
                        py,
                        "inject_request",
                        state,
                        scope_method,
                        scope_path,
                        scope_query,
                        headers,
                        path_map,
                        query_params,
                        body_bytes,
                        client_addr,
                        &param.oauth_scopes,
                    )?;
                    kwargs.set_item(param.name_pystr(py), req.bind(py))?;
                }
            }
            "inject_background_tasks" | "inject_response" | "inject_security_scopes" => {
                let obj = build_injected_object(
                    py,
                    param.kind.as_str(),
                    state,
                    scope_method,
                    scope_path,
                    scope_query,
                    headers,
                    path_map,
                    query_params,
                    body_bytes,
                    client_addr,
                    &param.oauth_scopes,
                )?;
                kwargs.set_item(param.name_pystr(py), obj.bind(py))?;
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
fn drain_background_tasks(py: Python<'_>, kwargs: &Bound<'_, PyDict>, params: &[ParamInfo]) {
    let mut seen_ids: std::collections::HashSet<usize> = std::collections::HashSet::new();
    // Drain the per-request SHARED BackgroundTasks (handler + every dependency add
    // to it) — a dep-injected instance lives in ``resolved``, not ``kwargs``, so the
    // kwargs scan below would miss it. Dedup by ptr against the kwargs scan.
    let shared = INJECTED_BACKGROUND_TASKS.with(|c| c.borrow().as_ref().map(|o| o.clone_ref(py)));
    if let Some(bg) = shared {
        let bg = bg.bind(py);
        seen_ids.insert(bg.as_ptr() as usize);
        let has_tasks = bg
            .getattr("_tasks")
            .ok()
            .and_then(|t| t.len().ok())
            .map(|n| n > 0)
            .unwrap_or(false);
        if has_tasks {
            let _ = bg.call_method0("run_sync");
        }
    }
    for param in params {
        if param.kind == "inject_background_tasks" {
            if let Ok(Some(bg_obj)) = kwargs.get_item(&param.name) {
                // Dedup across params — multiple inject_background_tasks
                // params may share one BackgroundTasks instance.
                let obj_id = bg_obj.as_ptr() as usize;
                if !seen_ids.insert(obj_id) {
                    continue;
                }
                let has_tasks = bg_obj
                    .getattr("_tasks")
                    .ok()
                    .and_then(|t| t.len().ok())
                    .map(|n| n > 0)
                    .unwrap_or(false);
                if !has_tasks {
                    continue;
                }
                // Run tasks SYNCHRONOUSLY while holding the GIL so the
                // response doesn't return before tasks execute —
                // matches Starlette's post-response event-loop drain
                // from a test-observable standpoint.
                let _ = bg_obj.call_method0("run_sync");
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
fn apply_injected_response(py: Python<'_>, response: &mut Response) {
    // Merge the per-request SHARED injected Response (set by the handler AND/OR
    // any dependency) onto the outgoing response. Reading the thread-local — not
    // handler kwargs — means a Response injected ONLY by a dependency still
    // applies (the old kwargs-only merge missed that, dropping dep-set headers,
    // e.g. on custom-response_class routes / include_router default-class chains).
    let obj = INJECTED_RESPONSE.with(|cell| cell.borrow().as_ref().map(|o| o.clone_ref(py)));
    if let Some(obj) = obj {
        let obj = obj.into_bound(py);
        // Apply the shell's status_code when the handler/dep SET one. The shell is
        // created with status_code=None (build_injected_object), so a successful
        // u16 extract means it was explicitly set — and it overrides the route-level
        // default (e.g. handler sets 200 to override a route ``status_code=201``).
        if let Ok(sc_attr) = obj.getattr("status_code") {
            if let Ok(sc) = sc_attr.extract::<u16>() {
                if let Ok(s) = StatusCode::from_u16(sc) {
                    *response.status_mut() = s;
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
                    if let Some((ks, _)) = crate::responses::extract_header_pair(&item) {
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
                    if let Some((ks, vs)) = crate::responses::extract_header_pair(&item) {
                        if let (Ok(hn), Ok(hv)) = (
                            HeaderName::try_from(ks.as_str()),
                            HeaderValue::from_str(&vs),
                        ) {
                            response.headers_mut().append(hn, hv);
                        }
                    }
                }
            }
        }
        // If the (shell-overridden) status is a no-body status, strip the body +
        // content-length: a dep/handler may have set 204/304 AFTER py_to_response
        // already rendered a body (FastAPI/Starlette send no body for these).
        let st = response.status();
        if st.as_u16() < 200 || st == StatusCode::NO_CONTENT || st == StatusCode::NOT_MODIFIED {
            *response.body_mut() = axum::body::Body::empty();
            response
                .headers_mut()
                .remove(axum::http::header::CONTENT_LENGTH);
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
                let err_type_str = d
                    .get_item("type")
                    .ok()
                    .flatten()
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
                if let Some(m) = d
                    .get_item("msg")
                    .ok()
                    .flatten()
                    .and_then(|v| v.extract::<String>().ok())
                {
                    obj.insert(
                        "msg".into(),
                        serde_json::Value::String(fastapi_normalize_error_msg(&m)),
                    );
                }
                let is_missing = obj
                    .get("type")
                    .and_then(|v| v.as_str())
                    .map(|s| s == "missing")
                    .unwrap_or(false);
                // FA parity: for combined-body ``missing`` errors, null
                // the ``input`` only when the missing field is AT THE
                // TOP of the combined body (loc = ["body", "<field>"]).
                // For nested missing fields (loc has more than 2
                // segments) preserve Pydantic's partial input so users
                // see what they sent. Without this,
                // ``test_tutorial002::test_post_missing_required_field_in_item``
                // sees ``None`` instead of ``{"name": "Foo"}``.
                let loc_len = obj
                    .get("loc")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                if strip_missing_input && is_missing && loc_len <= 2 {
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

/// Pick the validator for a ``body`` param: the cached SchemaValidator, else the
/// model class's ``__pydantic_validator__``, else the scalar ``TypeAdapter`` — the
/// last covers NON-model typed bodies (``list[Model]`` / ``dict[...]`` etc.) which
/// FastAPI validates against the body field's TypeAdapter just like a model body.
fn resolve_body_validator(py: Python<'_>, param: &ParamInfo) -> Option<Py<PyAny>> {
    if let Some(ref v) = param.cached_validator {
        return Some(v.clone_ref(py));
    }
    if let Some(ref mc) = param.model_class {
        if let Ok(v) = mc.bind(py).getattr("__pydantic_validator__") {
            return Some(v.unbind());
        }
    }
    // Non-model body: validate via the field's TypeAdapter (scalar_validator) —
    // structural containers (``list[Model]``, typed ``dict[K, V]``), bare ``dict``,
    // AND plain scalars (``float``/``int``/``str`` with constraints, e.g.
    // ``Body(allow_inf_nan=False)``). All run through the Content-Type-aware
    // validate_json/validate_python path, so strict_content_type is enforced (the
    // lax flag now reaches the adapter handler) and field constraints are checked.
    // (cached_validator / model_class are handled above.)
    param.scalar_validator.as_ref().map(|v| v.clone_ref(py))
}

/// Apply a parameter's default to the kwargs dict. Honors `has_default`:
/// when the marker declares `default=None`, we pass Python `None` explicitly
/// so the handler doesn't fall back to the function signature's default
/// (which would be the marker object itself).
fn apply_default<'py>(py: Python<'py>, kwargs: &Bound<'py, PyDict>, param: &ParamInfo) -> bool {
    if !param.has_default {
        return false;
    }
    match &param.default_value {
        Some(v) => {
            let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
        }
        None => {
            let _ = kwargs.set_item(param.name_pystr(py), py.None());
        }
    }
    true
}

/// Construct a `PyUploadFile` directly — no Python `UploadFile` wrapper needed
/// because `PyUploadFile` now implements the full async Starlette interface
/// (read/seek/close return `ImmediateBytes` / `ImmediateNone` awaitables).
/// `isinstance(f, UploadFile)` still works via the ABCMeta subclasshook on
/// the Python `UploadFile` class.
fn make_upload_file<'py>(py: Python<'py>, field: ParsedField) -> PyResult<Bound<'py, PyAny>> {
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
    /// BOUND METHOD ``validator._native.validate_json`` (the fused jiter
    /// parse+validate on a real BaseModel's SchemaValidator), pre-bound at
    /// ``build_router``. The hot path calls it directly — no Python frame, no
    /// ``_FABodyValidator.validate_json`` wrapper dispatch. On ANY error the
    /// caller re-runs the wrapper so error shapes stay FA-exact (json_invalid
    /// byte loc, model_attributes_type, combined-body per-field missing).
    /// ``None`` when the cached validator has no ``_native`` (combined body,
    /// ``_TypeAdapterProxy``, raw SchemaValidator).
    pub native_json_validator: Option<Py<PyAny>>,
    /// Param name INTERNED as a ``PyString`` at ``build_router``. Using it as
    /// the kwargs key skips a per-request PyString alloc AND enables CPython's
    /// pointer-compare kwarg matching when the handler is called. ``None`` only
    /// for ParamInfos that never went through ``build_router``.
    pub interned_name: Option<Py<pyo3::types::PyString>>,
    /// True when this is a PATH param annotated EXACTLY ``int`` or ``str`` with
    /// no constraints (set by introspection). The extractor fast-parses it in
    /// Rust (int: optional ``-`` + ASCII digits in i64 range; str: passthrough);
    /// any other shape or parse failure falls back to the TypeAdapter path so
    /// error bodies and lax coercions ("+7", "1_0", big ints) stay FA-exact.
    #[pyo3(get, set)]
    pub fast_path_coerce: bool,
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
    /// ``Depends(..., scope="function")`` (FastAPI 0.136). A function-scope yield
    /// dependency tears down BEFORE the response is sent (its post-yield raise
    /// becomes the response); the default ("request") scope tears down AFTER the
    /// response body. Only meaningful when ``is_generator_dep``.
    #[pyo3(get, set)]
    pub is_function_scope: bool,
    #[pyo3(get, set)]
    pub dep_input_names: Vec<(String, String)>,
    #[pyo3(get, set)]
    pub is_handler_param: bool,
    /// OAuth2 scopes accumulated down the ``Security(..., scopes=[...])`` chain for
    /// an ``inject_security_scopes`` param — the door builds ``SecurityScopes(
    /// scopes=...)`` from this. Empty for every other param kind.
    #[pyo3(get, set)]
    pub oauth_scopes: Vec<String>,
    /// Async-dep classification cache (0 unknown / 1 sync-fast / 2 needs-worker)
    /// for ``is_async_dep`` params — the per-dep counterpart of
    /// ``RouteState.handler_async_class``. ``Arc`` so clones (RouteState builds
    /// clone the params) share one converged classification per dep callable.
    pub dep_async_class: std::sync::Arc<std::sync::atomic::AtomicU8>,
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
            native_json_validator: self.native_json_validator.as_ref().map(|v| v.clone_ref(py)),
            interned_name: self.interned_name.as_ref().map(|v| v.clone_ref(py)),
            fast_path_coerce: self.fast_path_coerce,
            scalar_validator: self.scalar_validator.as_ref().map(|v| v.clone_ref(py)),
            alias: self.alias.clone(),
            dep_callable: self.dep_callable.as_ref().map(|v| v.clone_ref(py)),
            dep_callable_id: self.dep_callable_id,
            is_async_dep: self.is_async_dep,
            is_generator_dep: self.is_generator_dep,
            is_function_scope: self.is_function_scope,
            dep_input_names: self.dep_input_names.clone(),
            is_handler_param: self.is_handler_param,
            oauth_scopes: self.oauth_scopes.clone(),
            dep_async_class: self.dep_async_class.clone(),
        })
    }
}

#[pymethods]
impl ParamInfo {
    #[new]
    #[pyo3(signature = (name, kind, type_hint="str".to_string(), required=true, default_value=None, has_default=false, model_class=None, alias=None, dep_callable=None, dep_callable_id=None, is_async_dep=false, is_generator_dep=false, dep_input_names=vec![], is_handler_param=true, scalar_validator=None, oauth_scopes=vec![], is_function_scope=false, fast_path_coerce=false))]
    #[allow(clippy::too_many_arguments)]
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
        oauth_scopes: Vec<String>,
        is_function_scope: bool,
        fast_path_coerce: bool,
    ) -> Self {
        ParamInfo {
            name,
            kind,
            type_hint,
            required,
            default_value,
            has_default,
            model_class,
            scalar_validator,
            cached_validator: None, // Populated at startup by build_router
            native_json_validator: None, // Populated at startup by build_router
            interned_name: None,    // Populated at startup by build_router
            fast_path_coerce,
            alias,
            dep_callable,
            dep_callable_id,
            is_async_dep,
            is_generator_dep,
            is_function_scope,
            dep_input_names,
            is_handler_param,
            oauth_scopes,
            dep_async_class: std::sync::Arc::new(std::sync::atomic::AtomicU8::new(
                crate::handler_bridge::ASYNC_CLASS_UNKNOWN,
            )),
        }
    }
}

impl ParamInfo {
    /// The kwargs key for this param: the build-time interned ``PyString``
    /// (no alloc; CPython matches interned kwarg names by pointer compare in
    /// the handler call) — or a fresh PyString for the rare ParamInfo that
    /// never passed through ``build_router``.
    #[inline]
    fn name_pystr<'py>(&self, py: Python<'py>) -> Bound<'py, pyo3::types::PyString> {
        match &self.interned_name {
            Some(s) => s.bind(py).clone(),
            None => pyo3::types::PyString::new(py, &self.name),
        }
    }
}

/// Rust-side fast coercion for an UNCONSTRAINED ``int``/``str`` path param
/// (``param.fast_path_coerce``). Returns ``None`` for ANY shape outside the
/// strict fast lane — the caller falls back to the Pydantic TypeAdapter so
/// lax coercions ("+7", " 7", "1_0", "1.0", > i64 big ints) and 422 error
/// bodies stay FA-exact. The accepted int shape (optional leading ``-`` +
/// ASCII digits, i64 range) is a subset where Pydantic provably yields the
/// same value (incl. leading zeros: Pydantic lax parses "007" → 7).
#[inline]
fn fast_coerce_path_value(py: Python<'_>, raw: &str, type_hint: &str) -> Option<Py<PyAny>> {
    match type_hint {
        "str" => Some(pyo3::types::PyString::new(py, raw).into_any().unbind()),
        "int" => {
            let b = raw.as_bytes();
            let digits = match b.split_first() {
                Some((b'-', rest)) => rest,
                _ => b,
            };
            if digits.is_empty() || !digits.iter().all(|c| c.is_ascii_digit()) {
                return None;
            }
            raw.parse::<i64>()
                .ok()
                .map(|i| i.into_pyobject(py).expect("int").into_any().unbind())
        }
        _ => None,
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
    /// Route-level default status code (``@app.get(status_code=201)``). The door
    /// applies it as the default status for non-Response handler results; a
    /// handler/dep that sets ``response.status_code`` still overrides it.
    #[pyo3(get, set)]
    pub status_code: Option<u16>,
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
            status_code: self.status_code,
        })
    }
}

#[pymethods]
impl RouteInfo {
    #[new]
    #[pyo3(signature = (path, methods, handler, is_async=false, handler_name="".to_string(), params=vec![], is_websocket=false, status_code=None))]
    fn new(
        path: String,
        methods: Vec<String>,
        handler: Py<PyAny>,
        is_async: bool,
        handler_name: String,
        params: Vec<ParamInfo>,
        is_websocket: bool,
        status_code: Option<u16>,
    ) -> Self {
        RouteInfo {
            path,
            methods,
            handler,
            is_async,
            handler_name,
            params,
            is_websocket,
            status_code,
        }
    }
}

// ── Path conversion ───────────────────────────────────────────────────

/// Walk ``registration_order`` and return the methods of the first
/// route whose FastAPI-form pattern matches the candidate (also given
/// in axum/matchit form here — converted back to literal segments
/// internally). Mirrors Starlette's ``Router.app`` first-match-wins
/// semantics for OPTIONS / 405 fallbacks. ``None`` means the
/// candidate has no matching pattern in registration order, in which
/// case the caller falls back to the per-path declared methods.
fn first_pattern_match(
    registration_order: &[(String, Vec<String>)],
    candidate_axum_path: &str,
) -> Option<Vec<String>> {
    let candidate_segments: Vec<&str> = candidate_axum_path.trim_matches('/').split('/').collect();

    for (pattern, methods) in registration_order {
        if pattern_matches_axum_path(pattern, &candidate_segments) {
            return Some(methods.clone());
        }
    }
    None
}

/// Check whether a FastAPI-form pattern (``/items/{id}``,
/// ``/files/{path:path}``, etc.) matches the candidate path's
/// segments. Param segments (``{name}``) match any single segment;
/// catch-all (``{name:path}``) matches the rest. Literal segments
/// must match exactly, including against axum's ``{name}`` param
/// markers (a literal ``special`` would NOT match an axum-form
/// segment ``{id}`` on the candidate side because the candidate
/// segments come from the literal ``axum_path`` strings registered
/// in ``by_path``).
fn pattern_matches_axum_path(pattern: &str, candidate_segments: &[&str]) -> bool {
    let pattern_segments: Vec<&str> = pattern.trim_matches('/').split('/').collect();

    let mut pi = 0;
    let mut ci = 0;
    while pi < pattern_segments.len() && ci < candidate_segments.len() {
        let p = pattern_segments[pi];
        let c = candidate_segments[ci];

        let is_param = p.starts_with('{') && p.ends_with('}');
        if is_param {
            let inner = &p[1..p.len() - 1];
            if inner.ends_with(":path") || inner.starts_with('*') {
                // Catch-all matches the rest of the candidate.
                return true;
            }
            // Single-segment param matches whatever the candidate
            // has at this position (literal OR param marker — both
            // satisfy the pattern's ``one segment`` requirement).
            pi += 1;
            ci += 1;
            continue;
        }

        // Literal pattern segment must match candidate exactly. The
        // candidate may itself be an axum-form param marker
        // (``{id}``) when the registered ``axum_path`` was a pure
        // pattern; in that case the literal pattern doesn't match
        // (a literal route doesn't satisfy a param-only path's
        // first-match check, and pattern-vs-pattern is already
        // handled by the param branch above).
        if p != c {
            return false;
        }
        pi += 1;
        ci += 1;
    }

    pi == pattern_segments.len() && ci == candidate_segments.len()
}

fn convert_path(fastapi_path: &str) -> String {
    let mut result = String::with_capacity(fastapi_path.len());
    let mut chars = fastapi_path.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '{' {
            let mut param = String::new();
            for c in chars.by_ref() {
                if c == '}' {
                    break;
                }
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
    #[allow(dead_code)] // Wired at compile time, consumed by a future fast path.
    has_inject_response: bool,
    /// True when ANY ``inject_*`` param exists that ``inject_framework_objects``
    /// serves (request / background_tasks / response / security_scopes). When
    /// false the no-deps dispatch arms skip the call (and its per-param kind
    /// scan) entirely — the common body/path-only route case.
    has_inject_any: bool,
    /// True when a synthetic parameter-model extraction step exists (``pm_*``
    /// params) — precomputed so ``extract_params_to_pydict_full`` skips its
    /// per-request param-name scan + raw-dict builds on ordinary routes.
    has_param_model: bool,
    has_file_params: bool,
    has_form_params: bool,
    /// True when SOME code path reads the ``query_multi`` repeated-key multimap:
    /// a list-typed query param, a param-model route (reads the full raw_query),
    /// or a dependency (its list-query inputs go through ``extract_single_param``
    /// which reads ``query_multi``). When false, the second full query-string
    /// parse + per-key Vec allocs are skipped — the common case for body-only,
    /// scalar-query, and no-query routes.
    needs_query_multi: bool,
    has_http_middleware: bool,
    /// True when SOME consumer of the request-scope ContextVar exists for this
    /// app: a user ``@app.exception_handler`` entry OR an active Sentry client.
    /// When false, ``set_request_scope_ctxvar`` skips the endpoint/route
    /// getattrs, dict build, and the Python call entirely (the bench/common
    /// handler-only apps take this skip). Computed once at startup; the door
    /// rebuilds RouteState when ``app.exception_handlers`` changes (folded into
    /// ``_door_fingerprint``), so a late-registered handler is not missed.
    wants_request_scope: bool,
    /// True when the Python handler advertises
    /// ``_fastapi_turbo_defers_extraction_errors = True`` — the compile
    /// pipeline sets this on routes with `Depends(...)` so that
    /// ``HTTPException`` raised from a dep body wins over accumulated
    /// parameter-validation 422s (FA-normative precedence).
    defers_extraction_errors: bool,
    /// FA 0.120+ ``FastAPI(strict_content_type=False)`` — when True,
    /// JSON body parsing happens regardless of ``Content-Type`` header.
    lax_content_type: bool,
    /// The original APIRoute object — populated into
    /// ``request.scope["route"]`` so handlers can introspect the route.
    route_obj: Option<Py<PyAny>>,
    /// Route-level default status code (``status_code=201``); applied to
    /// non-Response handler results, overridable by a handler/dep-set
    /// ``response.status_code``.
    status_code: Option<u16>,
    /// Effective async-worker submit timeout, resolved ONCE at ``build_router``
    /// via ``_async_worker._default_timeout(app)`` (env var →
    /// ``app.worker_timeout`` → last-constructed fallback → None). The async
    /// dispatch arms pass it straight into ``submit_fast`` — no per-request
    /// ``APP_INSTANCE.read()`` + clone_ref, no kwargs dict, no env read under
    /// the GIL. Staleness is handled by the door: ``_door_fingerprint`` /
    /// re-registration rebuilds RouteState (and ``register_app_router`` /
    /// ``run_server`` set ``APP_INSTANCE`` before building), so each app's
    /// routes capture their OWN app's timeout — strictly better isolation than
    /// the old last-registered-wins per-request read.
    worker_timeout: Option<f64>,
    /// Async-handler classification cache (0 unknown / 1 sync-fast /
    /// 2 needs-worker) — per-route ``AtomicU8`` replacing the old process-global
    /// ``Mutex<HashMap>`` that every async request contended on.
    handler_async_class: std::sync::atomic::AtomicU8,
    // Note: body validation stays with Pydantic (Rust-backed) for 100% compatibility.
    // jsonschema crate can't handle custom validators, coercion, defaults, etc.
}

struct WsRouteState {
    handler: Py<PyAny>,
    is_async: bool,
}

// Shared WS dispatch — extracts scope info from the HTTP request parts and
// invokes the user's WebSocket handler via `handle_ws_upgrade`. Used by
// both the dedicated `ws_router` and the GET dispatcher we build when a WS
// route shares its path with HTTP routes (Strawberry GraphQLRouter).
async fn dispatch_ws(
    ws: WebSocketUpgrade,
    path_map: HashMap<String, String>,
    req_parts: &axum::http::request::Parts,
    handler: Py<PyAny>,
    is_async: bool,
) -> Response {
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
        .map(|(k, v)| (k.as_str().to_owned(), v.to_str().unwrap_or("").to_owned()))
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
    let client: Option<(String, u16)> = None;
    let ws_path_params: Vec<(String, String)> = path_map
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
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
    handle_ws_upgrade(ws, handler, is_async, scope).await
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
    // The 5th element preserves the optional GET RouteState so a later
    // WS-route collision can rewire the GET to a WS-upgrade dispatcher
    // (Strawberry GraphQLRouter: /graphql serves GET/POST AND a WS sub).
    let mut by_path: Vec<(
        String,
        MethodRouter,
        Vec<String>,
        bool,
        Option<Arc<RouteState>>,
    )> = Vec::new();
    // Paths with a WS route. Collision-free paths go on `ws_router`;
    // paths shared with HTTP get a combined GET dispatcher on the main
    // router (axum can't merge two routers when both have method-router
    // fallbacks on the same path).
    let mut ws_by_path: HashMap<String, Arc<WsRouteState>> = HashMap::new();

    // Snapshot the original FastAPI route registration order with
    // ``(pattern, methods)`` pairs so we can compute first-match-wins
    // Allow headers below. Patterns are in FastAPI ``/items/{id}``
    // form (NOT axum's matchit syntax). Built BEFORE the per-route
    // loop consumes ``routes``.
    let registration_order: Vec<(String, Vec<String>)> = routes
        .iter()
        .map(|r| {
            (
                r.path.clone(),
                r.methods.iter().map(|m| m.to_uppercase()).collect(),
            )
        })
        .collect();

    // Resolve the effective async-worker submit timeout ONCE for this build.
    // All three build entry points (``run_server``, ``register_app_router``,
    // the cluster worker) set ``APP_INSTANCE`` to the app being registered
    // immediately before assembling the router, and the door re-registers
    // (rebuilding every RouteState) when ``_door_fingerprint`` changes — so
    // the value captured here is the OWNING app's timeout for the lifetime
    // of these RouteStates.
    let worker_timeout: Option<f64> = Python::attach(|py| {
        let app = APP_INSTANCE
            .read()
            .ok()
            .and_then(|g| g.as_ref().map(|a| a.clone_ref(py)));
        let resolver = py
            .import("fastapi_turbo._async_worker")
            .and_then(|m| m.getattr("_default_timeout"))
            .ok()?;
        let resolved = match app {
            Some(a) => resolver.call1((a.bind(py),)).ok()?,
            None => resolver.call1((py.None(),)).ok()?,
        };
        resolved.extract::<Option<f64>>().ok().flatten()
    });

    for route in routes {
        let axum_path = convert_path(&route.path);

        if route.is_websocket {
            let ws_state = Arc::new(WsRouteState {
                handler: Python::attach(|py| route.handler.clone_ref(py)),
                is_async: route.is_async,
            });
            ws_by_path.insert(axum_path.clone(), ws_state);
            continue;
        }

        // Pre-compute flags at startup to avoid per-request scanning
        let has_body = route.params.iter().any(|p| p.kind == "body");
        let has_header = route
            .params
            .iter()
            .any(|p| p.kind == "header" || p.kind == "cookie");
        let has_dep = route.params.iter().any(|p| p.kind == "dependency");
        let has_any = !route.params.is_empty();
        let has_file = route.params.iter().any(|p| p.kind == "file");
        let has_form = route.params.iter().any(|p| p.kind == "form");
        let has_inj_req = route.params.iter().any(|p| p.kind == "inject_request");
        let has_inj_bg = route
            .params
            .iter()
            .any(|p| p.kind == "inject_background_tasks");
        let has_inj_resp = route.params.iter().any(|p| p.kind == "inject_response");
        let has_inj_scopes = route
            .params
            .iter()
            .any(|p| p.kind == "inject_security_scopes");
        let has_inj_any = has_inj_req || has_inj_bg || has_inj_resp || has_inj_scopes;
        let has_param_model = route
            .params
            .iter()
            .any(|p| p.name.starts_with("pm_") && p.name.contains("__"));
        // ``query_multi`` (the repeated-key multimap) is only read by: list-typed
        // query params (3093/3794), param-model routes that emit ``raw_query``
        // (3663, ``pm_`` params), and dependencies whose list-query inputs go
        // through ``extract_single_param`` (3794). Anything else — body-only,
        // scalar query, no query — never touches it, so skip the second parse.
        let needs_query_multi = has_dep
            || route.params.iter().any(|p| {
                p.name.starts_with("pm_") || (p.kind == "query" && p.type_hint.starts_with("list_"))
            });

        let state = Python::attach(|py| {
            // Pre-cache pydantic validators at startup (saves ~0.3μs getattr per POST request)
            let mut params = route.params.clone();
            // Build an FA-compatible body validator that parses JSON then
            // calls `validate_python(data, from_attributes=True)`. This
            // matches stock FastAPI's error shape (`model_attributes_type`
            // instead of `model_type`, FA-style messages, no ctx).
            let fa_factory = py
                .import("fastapi_turbo._door_support")
                .and_then(|m| m.getattr("_make_fa_body_validator"))
                .ok();
            for param in &mut params {
                // Intern every param name once — the per-request kwargs
                // ``set_item`` reuses the same PyString object (no alloc,
                // pointer-compare kwarg match in the handler call).
                param.interned_name = Some(pyo3::types::PyString::intern(py, &param.name).unbind());
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
                        // Pre-bind the fused native ``SchemaValidator.validate_json``
                        // (``_FABodyValidator._native``) so the hot path skips the
                        // Python-frame wrapper entirely. Left None for combined-body /
                        // ``_TypeAdapterProxy`` wrappers (``_native is None``) and for
                        // raw SchemaValidators (no ``_native`` attribute).
                        param.native_json_validator = cached.as_ref().and_then(|v| {
                            let native = v.bind(py).getattr("_native").ok()?;
                            if native.is_none() {
                                return None;
                            }
                            native.getattr("validate_json").ok().map(|m| m.unbind())
                        });
                        param.cached_validator = cached;
                    } else if param.cached_validator.is_none() && param.scalar_validator.is_some() {
                        // NON-model body (typed dict / container / scalar via the
                        // field TypeAdapter): wrap the adapter in the FA-shaping
                        // two-pass validator. Real FastAPI parses the body with
                        // stdlib ``json`` FIRST and only then validates in Python
                        // mode — so malformed JSON must yield FA's hardcoded
                        // ``json_invalid`` shape (loc=("body", pos), msg "JSON
                        // decode error", input={}, ctx.error=<json.msg>), NOT
                        // pydantic-core's own JSON-parse error (loc=("body",),
                        // "Invalid JSON: ...") which the raw ``TypeAdapter
                        // .validate_json`` emits (R2 deep-validation J001).
                        if let Ok(ref factory) = py
                            .import("fastapi_turbo._door_support")
                            .and_then(|m| m.getattr("_make_fa_body_validator_from_adapter"))
                        {
                            if let Some(ref adapter) = param.scalar_validator {
                                if let Ok(v) = factory.call1((adapter.bind(py),)) {
                                    if !v.is_none() {
                                        param.cached_validator = Some(v.unbind());
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // Does ANY consumer of the request-scope ctxvar exist for this app?
            // Consumers: a non-empty ``app.exception_handlers`` (user
            // ``@app.exception_handler`` entries) OR an active Sentry client
            // (``_fastapi_turbo_sentry_installed``). When neither holds, the
            // per-request scope set serves nobody and is skipped.
            let wants_request_scope = current_app(py)
                .map(|app| {
                    let eh_nonempty = app
                        .getattr(py, "exception_handlers")
                        .ok()
                        .and_then(|eh| eh.bind(py).len().ok())
                        .map(|n| n > 0)
                        // If we can't read exception_handlers, keep the old
                        // (populating) behaviour to stay safe.
                        .unwrap_or(true);
                    let sentry = app
                        .getattr(py, "_fastapi_turbo_sentry_installed")
                        .and_then(|v| v.extract::<bool>(py))
                        .unwrap_or(false);
                    eh_nonempty || sentry
                })
                // No bound app (shouldn't happen on the door path) → be safe.
                .unwrap_or(true);

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
                has_inject_any: has_inj_any,
                has_param_model,
                has_file_params: has_file,
                has_form_params: has_form,
                needs_query_multi,
                wants_request_scope,
                has_http_middleware: route
                    .handler
                    .getattr(py, "_has_http_middleware")
                    .and_then(|v| v.extract::<bool>(py))
                    .unwrap_or(false),
                defers_extraction_errors: route
                    .handler
                    .getattr(py, "_fastapi_turbo_defers_extraction_errors")
                    .and_then(|v| v.extract::<bool>(py))
                    .unwrap_or(false),
                lax_content_type: route
                    .handler
                    .getattr(py, "_fastapi_turbo_lax_content_type")
                    .and_then(|v| v.extract::<bool>(py))
                    .unwrap_or(false),
                route_obj: route.handler.getattr(py, "_fastapi_turbo_route_obj").ok(),
                status_code: route.status_code,
                worker_timeout,
                // Async-inline registration (FASTAPI_TURBO_ASYNC_INLINE) pre-marks
                // known-suspending handlers with ``_fastapi_turbo_needs_worker`` so
                // the door NEVER probes them with send(None) — the first request
                // already takes the inline worker-loop path. Flag off: plain
                // UNKNOWN, byte-identical to the classic build.
                handler_async_class: std::sync::atomic::AtomicU8::new(
                    if async_inline_enabled()
                        && route
                            .handler
                            .getattr(py, "_fastapi_turbo_needs_worker")
                            .and_then(|v| v.extract::<bool>(py))
                            .unwrap_or(false)
                    {
                        crate::handler_bridge::ASYNC_CLASS_NEEDS_WORKER
                    } else {
                        crate::handler_bridge::ASYNC_CLASS_UNKNOWN
                    },
                ),
            })
        });

        let mut method_router: Option<MethodRouter> = None;
        // Track which methods this route declares (for Allow header + auto-OPTIONS).
        let declared_methods: Vec<String> =
            route.methods.iter().map(|m| m.to_uppercase()).collect();
        let has_explicit_options = declared_methods.iter().any(|m| m == "OPTIONS");

        for method_str in &route.methods {
            let s = state.clone();
            let m = method_str.to_uppercase();

            let handler_fn = move |path_params: Option<Path<HashMap<String, String>>>,
                                   query_params: Query<HashMap<String, String>>,
                                   request: Request<Body>| {
                let state = s.clone(); // Arc::clone — just refcount, no GIL
                async move { handle_request(state, path_params, query_params, request).await }
            };

            let mr = match m.as_str() {
                "GET" => get(handler_fn),
                "POST" => post(handler_fn),
                "PUT" => put(handler_fn),
                "DELETE" => delete(handler_fn),
                "PATCH" => patch(handler_fn),
                "HEAD" => head(handler_fn),
                "OPTIONS" => axum::routing::options(handler_fn),
                "TRACE" => axum::routing::on(axum::routing::MethodFilter::TRACE, handler_fn),
                other => {
                    eprintln!("fastapi-turbo: unsupported HTTP method '{other}', skipping");
                    continue;
                }
            };

            method_router = Some(match method_router {
                Some(existing) => existing.merge(mr),
                None => mr,
            });
        }

        if let Some(mr) = method_router {
            let get_state_opt = if declared_methods.iter().any(|m| m == "GET") {
                Some(state.clone())
            } else {
                None
            };
            // Merge with any existing accumulator for this path so that
            // `@app.get("/x")` and `@app.post("/x")` end up on one MethodRouter.
            if let Some(entry) = by_path.iter_mut().find(|(p, _, _, _, _)| p == &axum_path) {
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
                    if entry.4.is_none() {
                        entry.4 = get_state_opt;
                    }
                } else {
                    // Mixed case: some methods new, some duplicate. Skip
                    // the whole route since we can't split the
                    // MethodRouter. Rare; a warning helps surface it.
                    eprintln!(
                        "fastapi-turbo: duplicate method(s) {dup:?} on path {axum_path:?}, skipping second registration"
                    );
                }
            } else {
                by_path.push((
                    axum_path,
                    mr,
                    declared_methods,
                    has_explicit_options,
                    get_state_opt,
                ));
            }
        }
    }

    // Attach 405 fallback per path. Matches FastAPI/Starlette exactly:
    //   - body: {"detail": "Method Not Allowed"}
    //   - Allow header: methods of the FIRST-REGISTERED route whose
    //     pattern matches this path. Matters for overlapping literal/
    //     param routes: ``/items/{id}`` (GET) registered before
    //     ``/items/special`` (POST) means OPTIONS /items/special must
    //     report ``Allow: GET`` (the {id} route wins by registration
    //     order, even though /items/special is more specific). matchit
    //     picks the most-specific match, which diverges from
    //     Starlette's first-match-wins; computing the Allow header
    //     here in registration order restores parity.
    //   - OPTIONS and HEAD on a GET-only route both return 405, matching
    //     Starlette. If the user wants OPTIONS (CORS preflight), they should
    //     mount CORSMiddleware or declare OPTIONS explicitly.
    for (path, mut mr, declared, _had_options, get_state) in by_path {
        // For each REGISTERED path in by_path, find the first
        // registration whose pattern matches this concrete path
        // string and use ITS methods. ``path`` here is in axum's
        // matchit syntax; we need to strip it back to FastAPI form
        // for the comparison. The simplest reliable check: build the
        // FastAPI-form path back from the original registration's
        // axum_path, find first whose pattern matches the literal
        // segments of `path` (treating param segments as wildcards
        // in BOTH directions).
        let allow_methods: Vec<String> =
            if let Some(first_match) = first_pattern_match(&registration_order, &path) {
                first_match
            } else {
                declared.clone()
            };
        let mut seen = std::collections::HashSet::new();
        let mut allow: Vec<String> = allow_methods.clone();
        allow.retain(|m| seen.insert(m.clone()));
        let allow_header = allow.join(", ");

        // WS + HTTP collide on the same path (Strawberry GraphQLRouter:
        // GET + POST http queries AND subscription WebSocket at /graphql).
        // We can't merge the WS MethodRouter (an `any` handler with a
        // fallback) with the HTTP MethodRouter (specific methods + 405
        // fallback) — axum panics when both sides have a fallback.
        // Rebuild this path's method router from scratch with a combined
        // GET dispatcher that delegates to the WS bridge on upgrade
        // requests and to the original HTTP GET otherwise.
        if let (Some(ws_state), Some(s)) = (ws_by_path.remove(&path), get_state.as_ref()) {
            let mut new_mr: MethodRouter = MethodRouter::new();
            for m in &declared {
                if m == "GET" {
                    continue;
                }
                let state_clone = s.clone();
                let handler_fn = move |path_params: Option<Path<HashMap<String, String>>>,
                                       query_params: Query<HashMap<String, String>>,
                                       request: Request<Body>| {
                    let state = state_clone.clone();
                    async move { handle_request(state, path_params, query_params, request).await }
                };
                let piece = match m.as_str() {
                    "POST" => post(handler_fn),
                    "PUT" => put(handler_fn),
                    "DELETE" => delete(handler_fn),
                    "PATCH" => patch(handler_fn),
                    "HEAD" => head(handler_fn),
                    "OPTIONS" => axum::routing::options(handler_fn),
                    "TRACE" => axum::routing::on(axum::routing::MethodFilter::TRACE, handler_fn),
                    _ => continue,
                };
                new_mr = new_mr.merge(piece);
            }
            let http_get_state = s.clone();
            // Dispatcher: WS upgrade → WS bridge; else HTTP GET handler.
            // Axum's `WebSocketUpgrade` extractor wants to own the
            // connection, so it conflicts with taking `Request` alongside
            // it. Instead, detect the WS handshake headers on the Request
            // and build a WebSocketUpgrade via `from_request_parts` only
            // when needed.
            let get_handler = move |path_params: Option<Path<HashMap<String, String>>>,
                                    query_params: Query<HashMap<String, String>>,
                                    request: Request<Body>| {
                let ws_state = ws_state.clone();
                let http_state = http_get_state.clone();
                async move {
                    let is_ws_upgrade = {
                        let h = request.headers();
                        let conn_upgrade = h
                            .get(axum::http::header::CONNECTION)
                            .and_then(|v| v.to_str().ok())
                            .map(|v| {
                                v.to_ascii_lowercase()
                                    .split(',')
                                    .any(|p| p.trim() == "upgrade")
                            })
                            .unwrap_or(false);
                        let upgrade_ws = h
                            .get(axum::http::header::UPGRADE)
                            .and_then(|v| v.to_str().ok())
                            .map(|v| v.eq_ignore_ascii_case("websocket"))
                            .unwrap_or(false);
                        conn_upgrade && upgrade_ws
                    };
                    if is_ws_upgrade {
                        let (mut parts, _body) = request.into_parts();
                        use axum::extract::FromRequestParts;
                        match <WebSocketUpgrade as FromRequestParts<()>>::from_request_parts(
                            &mut parts,
                            &(),
                        )
                        .await
                        {
                            Ok(ws) => {
                                let path_map = path_params
                                    .as_ref()
                                    .map(|Path(m)| m.clone())
                                    .unwrap_or_default();
                                let h = Python::attach(|py| ws_state.handler.clone_ref(py));
                                return dispatch_ws(ws, path_map, &parts, h, ws_state.is_async)
                                    .await;
                            }
                            Err(rej) => return rej.into_response(),
                        }
                    }
                    handle_request(http_state, path_params, query_params, request).await
                }
            };
            new_mr = new_mr.merge(get(get_handler));
            mr = new_mr;
        }

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

        // Same explicit-405 override for OPTIONS. Axum's MethodRouter
        // has built-in 405 handling that uses ITS OWN registered-
        // methods list as the Allow header — so when matchit picks
        // ``/items/special`` (POST only) for an OPTIONS request,
        // axum auto-emits ``Allow: POST`` regardless of our computed
        // first-match-wins value. The ``.fallback(...)`` set below
        // doesn't fire on a known HTTP method (axum returns 405 from
        // its method-table first); the explicit ``mr.options(...)``
        // here is what actually delivers the parity-correct Allow
        // header to the client.
        if !declared.iter().any(|m| m == "OPTIONS") {
            let h = allow_header.clone();
            mr = mr.options(move || {
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

    // Register remaining WS-only paths (no HTTP counterpart) on the WS
    // sub-router — merged into the main router *after* CORS/compression
    // middleware so the 101 upgrade response stays untouched.
    for (path, ws_state) in ws_by_path {
        ws_router = ws_router.route(
            &path,
            any(
                move |ws: WebSocketUpgrade,
                      path_params: Option<Path<HashMap<String, String>>>,
                      req_parts: axum::http::request::Parts| {
                    let state = ws_state.clone();
                    async move {
                        let h = Python::attach(|py| state.handler.clone_ref(py));
                        let is_a = state.is_async;
                        let path_map = path_params.map(|Path(m)| m).unwrap_or_default();
                        dispatch_ws(ws, path_map, &req_parts, h, is_a).await
                    }
                },
            ),
        );
    }

    (router, ws_router)
}

/// Public entry: attach the FastAPI-style 404 fallback to a router. Called at
/// the top level after middleware and WS branches have been merged, so the
/// fallback fires only when nothing else matched.
pub fn with_not_found_fallback(router: Router) -> Router {
    router.fallback(
        |req: axum::http::Request<axum::body::Body>| async move { dispatch_404(req).await },
    )
}

/// Python callable supplied by ``run_server(not_found_handler=...)``.
/// Expected signature: ``fn(method: str, path: str) -> bytes`` where the
/// returned bytes is a ready-to-send JSON body. The shim in
/// ``applications.py`` wraps the user's handler into this shape.
pub static NOT_FOUND_HANDLER: std::sync::RwLock<Option<Py<PyAny>>> = std::sync::RwLock::new(None);

async fn dispatch_404(req: axum::http::Request<axum::body::Body>) -> Response {
    let has_handler = NOT_FOUND_HANDLER
        .read()
        .ok()
        .map(|g| g.is_some())
        .unwrap_or(false);
    if has_handler {
        let path = req.uri().path().to_string();
        let method = req.method().as_str().to_string();
        let query = req.uri().query().unwrap_or("").to_string();
        // Capture request headers as (bytes, bytes) tuples so the
        // Python-side middleware chain can reconstruct the ASGI scope —
        // ``SentryAsgiMiddleware``, ``SessionMiddleware``, etc. read
        // scope["headers"] to assemble the request context. ~1μs cost,
        // only on 404s.
        let headers_list: Vec<(Vec<u8>, Vec<u8>)> = req
            .headers()
            .iter()
            .map(|(k, v)| (k.as_str().as_bytes().to_vec(), v.as_bytes().to_vec()))
            .collect();
        let out = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> Option<(u16, Vec<u8>, Vec<(String, String)>)> {
                let guard = NOT_FOUND_HANDLER.read().ok()?;
                let handler = guard.as_ref()?;
                // Try the extended 4-arg signature first
                // (method, path, query, headers_list). Fall back to the
                // 2-arg shape for handlers that haven't been upgraded.
                let hdrs_py = pyo3::types::PyList::empty(py);
                for (k, v) in &headers_list {
                    let _ = hdrs_py.append((
                        pyo3::types::PyBytes::new(py, k),
                        pyo3::types::PyBytes::new(py, v),
                    ));
                }
                let result = handler
                    .call1(
                        py,
                        (method.as_str(), path.as_str(), query.as_str(), hdrs_py),
                    )
                    .or_else(|_| handler.call1(py, (method.as_str(), path.as_str())))
                    .ok()?;
                // Expected shape: (status: int, body: bytes) OR
                // (status, body, [(hdr_name, hdr_val), ...])
                if let Ok(tup) = result.extract::<(u16, Vec<u8>, Vec<(String, String)>)>(py) {
                    Some(tup)
                } else if let Ok((status, body)) = result.extract::<(u16, Vec<u8>)>(py) {
                    Some((status, body, Vec::new()))
                } else if let Ok(bytes) = result.extract::<Vec<u8>>(py) {
                    Some((404, bytes, Vec::new()))
                } else {
                    None
                }
            })
        })
        .await
        .ok()
        .flatten();
        if let Some((status, body, extra_headers)) = out {
            let mut builder = axum::response::Response::builder()
                .status(StatusCode::from_u16(status).unwrap_or(StatusCode::NOT_FOUND));
            let mut has_ct = false;
            for (k, v) in &extra_headers {
                if k.eq_ignore_ascii_case("content-type") {
                    has_ct = true;
                }
                builder = builder.header(k, v);
            }
            if !has_ct {
                builder = builder.header("content-type", "application/json");
            }
            return builder.body(axum::body::Body::from(body)).unwrap();
        }
    }
    axum::response::Response::builder()
        .status(StatusCode::NOT_FOUND)
        .header("content-type", "application/json")
        .body(axum::body::Body::from(r#"{"detail":"Not Found"}"#))
        .unwrap()
}

// ═══ FASTAPI_TURBO_ASYNC_INLINE (E): drive async requests on the worker loop ═══
//
// The classic needs-worker path builds the coroutine on the tokio thread,
// ships it to the worker loop via `_async_worker.submit_fast`, and then BLOCKS
// the tokio worker thread on a `threading.Event` for the whole request
// (~25 μs/submit, one parked OS thread per in-flight async request).
//
// The inline path instead enqueues a small Rust pyclass job onto the loop via
// `call_soon_threadsafe` and has the tokio task await a `oneshot::Receiver` —
// zero blocked threads, zero Event handoff. Parameter extraction, framework
// injection, and coroutine creation all run ON the loop thread (one GIL
// context); response conversion runs back on the tokio side (the conversion
// helpers read per-request thread-locals and `create_streaming_response`
// needs a tokio runtime context — neither is loop-thread-safe).
//
// Opt-in via `FASTAPI_TURBO_ASYNC_INLINE=1`. v1 scope: async handlers without
// dependencies, no http-middleware chain, no file/form params, and only once
// the route has classified as ASYNC_CLASS_NEEDS_WORKER (UNKNOWN/SYNC_FAST
// requests keep the classic probe path, which converges the route here after
// its first observed suspension).

/// Process-wide flag: `FASTAPI_TURBO_ASYNC_INLINE=1` (read once).
fn async_inline_enabled() -> bool {
    static FLAG: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *FLAG.get_or_init(|| {
        std::env::var("FASTAPI_TURBO_ASYNC_INLINE")
            .map(|v| matches!(v.trim(), "1" | "true" | "True" | "TRUE" | "yes" | "on"))
            .unwrap_or(false)
    })
}

/// Everything the loop-side job needs, moved out of `handle_request`'s locals.
/// All fields are owned/thread-agnostic — no borrow of the axum request
/// survives into the loop thread.
struct InlineParts {
    path_map: HashMap<String, String>,
    query_params: HashMap<String, String>,
    query_multi: HashMap<String, Vec<String>>,
    headers: Option<HeaderMap>,
    /// Content-Type carrier for the body arm (refcounted `Bytes` clone; the
    /// extractor reads it via `.to_str()` exactly like the classic call sites).
    content_type: Option<HeaderValue>,
    body_bytes: bytes::Bytes,
    body_json: Option<serde_json::Value>,
    scope_method: Option<String>,
    scope_path: Option<String>,
    scope_query: Option<String>,
    client_addr: Option<SocketAddr>,
    /// Shipped explicitly (NOT via the tokio-side thread-local) — the job seeds
    /// the loop thread's `REQUEST_DISCONNECT_FLAG` from it.
    disconnect_flag: Option<Py<PyAny>>,
}

/// What the loop thread sends back to the awaiting tokio task.
enum LoopOutcome {
    /// A fully-built response (Rust-side validation 422/400 — no conversion needed).
    Ready(Response),
    /// Handler completed: convert `obj` on the tokio side with the shipped
    /// per-request context (kwargs for the background-task drain, the injected
    /// Response/BackgroundTasks shells for merge/drain).
    Result {
        obj: Py<PyAny>,
        kwargs: Py<PyDict>,
        injected_bg: Option<Py<PyAny>>,
        injected_resp: Option<Py<PyAny>>,
    },
    /// Handler (or extraction/injection plumbing) raised — converted via
    /// `pyerr_to_response` on the tokio side (same arm the classic path uses,
    /// including the TimeoutError→504 mapping and 500-capture-onto-app).
    Error(PyErr),
}

/// Shared reply slot: whichever of {done-callback, timeout timer} takes the
/// sender first wins; the loser observes `None` and stands down.
type InlineReply = Arc<std::sync::Mutex<Option<tokio::sync::oneshot::Sender<LoopOutcome>>>>;

fn send_inline_outcome(reply: &InlineReply, outcome: LoopOutcome) {
    if let Some(tx) = reply.lock().ok().and_then(|mut g| g.take()) {
        // Send failure = receiver dropped (client disconnected) — the outcome
        // (and its Py refs) drop here on the loop thread under the GIL.
        let _ = tx.send(outcome);
    }
}

/// Enqueued via `loop.call_soon_threadsafe(job)`; `__call__` runs on the loop
/// thread under the loop's GIL as a plain callback. The whole method is
/// synchronous (no awaits), so loop-thread TL use inside it is race-free.
#[pyclass]
struct InlineJob {
    state: Arc<RouteState>,
    parts: Option<Box<InlineParts>>,
    reply: InlineReply,
}

#[pymethods]
impl InlineJob {
    fn __call__(&mut self, py: Python<'_>) {
        let Some(parts) = self.parts.take() else {
            return;
        };
        run_inline_job(py, &self.state, *parts, &self.reply);
    }
}

/// Loop-thread body of the job: ctxvar seed → extraction → injection →
/// coroutine → `loop.create_task` → completer wiring (+ optional timeout timer).
fn run_inline_job(
    py: Python<'_>,
    state: &Arc<RouteState>,
    parts: InlineParts,
    reply: &InlineReply,
) {
    // Loop-thread TL guard: the single worker loop interleaves EVERY in-flight
    // request between this synchronous block and the done-callback, so the
    // per-request thread-locals must never survive past this function.
    let _guard = DisconnectFlagGuard;
    // Request-scope ContextVar: `Handle._run` executes this callback inside the
    // context copied at `call_soon_threadsafe` time; setting the ctxvar here
    // mutates that context, and `loop.create_task` below snapshots the current
    // (mutated) context — so the request scope propagates into the handler
    // task exactly like the classic tokio-thread set → `_kickoff` chain.
    set_request_scope_ctxvar(
        py,
        &parts.scope_method,
        &parts.scope_path,
        &parts.scope_query,
        state,
    );
    if let Some(flag) = parts.disconnect_flag {
        REQUEST_DISCONNECT_FLAG.with(|f| *f.borrow_mut() = Some(flag));
    }
    let body_json_opt = if state.has_body_params {
        parts.body_json.as_ref()
    } else {
        None
    };
    // v1 gate excludes file/form routes, so there are never multipart fields.
    let mut multipart_fields: Option<HashMap<String, Vec<ParsedField>>> = None;
    let kwargs = match extract_params_to_pydict_full(
        py,
        &state.params,
        &parts.path_map,
        &parts.query_params,
        &parts.query_multi,
        &parts.headers,
        parts.content_type.as_ref().and_then(|v| v.to_str().ok()),
        &body_json_opt,
        &parts.body_bytes,
        &mut multipart_fields,
        state.defers_extraction_errors,
        state.lax_content_type,
        state.has_param_model,
    ) {
        Ok(kw) => kw,
        Err(resp) => return send_inline_outcome(reply, LoopOutcome::Ready(resp)),
    };
    if let Err(e) = inject_framework_objects(
        py,
        &kwargs,
        state,
        &parts.scope_method,
        &parts.scope_path,
        &parts.scope_query,
        &parts.headers,
        &parts.path_map,
        &parts.query_params,
        &parts.body_bytes,
        &parts.client_addr,
    ) {
        return send_inline_outcome(reply, LoopOutcome::Error(e));
    }
    // Take the injected shells OUT of the loop TLs now (the guard would clear
    // them anyway) — they ride to the tokio side on the completer instead.
    // The shared Request is only dropped (it already rode into kwargs);
    // leaving it would leak it to the NEXT request served on this loop thread.
    let injected_resp = INJECTED_RESPONSE.with(|c| c.borrow_mut().take());
    let injected_bg = INJECTED_BACKGROUND_TASKS.with(|c| c.borrow_mut().take());
    let _ = INJECTED_REQUEST.with(|c| c.borrow_mut().take());
    // Build the coroutine (body not executed at creation).
    let coro = match state.handler.call(py, (), Some(&kwargs)) {
        Ok(c) => c,
        Err(e) => return send_inline_outcome(reply, LoopOutcome::Error(e)),
    };
    let Some(loop_obj) = crate::handler_bridge::worker_loop() else {
        let _ = coro.call_method0(py, "close");
        return send_inline_outcome(
            reply,
            LoopOutcome::Error(pyo3::exceptions::PyRuntimeError::new_err(
                "async worker loop not initialized",
            )),
        );
    };
    let Some(runner) = crate::handler_bridge::inline_runner() else {
        let _ = coro.call_method0(py, "close");
        return send_inline_outcome(
            reply,
            LoopOutcome::Error(pyo3::exceptions::PyRuntimeError::new_err(
                "_inline_runner not initialized",
            )),
        );
    };
    let send = match Py::new(
        py,
        InlineSend {
            reply: reply.clone(),
            kwargs: kwargs.unbind(),
            injected_bg,
            injected_resp,
            timer: std::sync::Mutex::new(None),
            task: std::sync::Mutex::new(None),
            timeout_secs: state.worker_timeout,
        },
    ) {
        Ok(c) => c,
        Err(e) => {
            let _ = coro.call_method0(py, "close");
            return send_inline_outcome(reply, LoopOutcome::Error(e));
        }
    };
    // Wrap the handler coroutine in `_inline_runner(coro, send)`: the oneshot
    // fires as the LAST statement of the task body — same loop iteration the
    // handler completes in. The old `add_done_callback(completer)` wiring
    // delivered completion via `loop.call_soon`, i.e. one extra loop pass per
    // request (the E-path's "second hop", audited).
    let runner_coro = match runner.call1(py, (coro.bind(py), send.bind(py))) {
        Ok(rc) => rc,
        Err(e) => {
            let _ = coro.call_method0(py, "close");
            return send_inline_outcome(reply, LoopOutcome::Error(e));
        }
    };
    let task = match loop_obj.call_method1(py, "create_task", (runner_coro.bind(py),)) {
        Ok(t) => t,
        Err(e) => {
            let _ = runner_coro.call_method0(py, "close");
            return send_inline_outcome(reply, LoopOutcome::Error(e));
        }
    };
    // NOTE: with the eager task factory the runner may have ALREADY completed
    // (and sent the outcome) inside `create_task` — the timer/task wiring
    // below then arms against a taken sender, which is harmless: `_timeout`
    // and disconnect-cancel both no-op once the reply slot is empty.
    // Timeout: schedule `_timeout` on the loop. Delivering the 504 AT the
    // deadline (not from the cancelled task) preserves `submit_fast`
    // semantics — a handler that swallows CancelledError must not convert a
    // guaranteed 504 into a late 200.
    if let Some(t) = state.worker_timeout {
        if let Ok(cb) = send.bind(py).getattr("_timeout") {
            if let Ok(timer) = loop_obj.call_method1(py, "call_later", (t, cb)) {
                let already_done = reply.lock().map(|g| g.is_none()).unwrap_or(false);
                if already_done {
                    // Eager completion raced ahead of the arming — don't
                    // leave a live Handle parked until the deadline.
                    let _ = timer.call_method0(py, "cancel");
                } else if let Ok(mut slot) = send.borrow(py).timer.lock() {
                    *slot = Some(timer);
                }
            }
        }
    }
    let send_ref = send.borrow(py);
    if let Ok(mut slot) = send_ref.task.lock() {
        *slot = Some(task.clone_ref(py));
    }
    drop(send_ref);
}

/// ASYNC_INLINE completion sink. `__call__` is invoked by the Python
/// `_inline_runner` as the LAST statement of the handler task (same loop
/// iteration the handler completes in — no done-callback `call_soon` hop);
/// `_timeout` is the `call_later` timer target. Both run on the loop thread;
/// `oneshot::Sender::send` is non-blocking, so both are loop-safe.
#[pyclass]
struct InlineSend {
    reply: InlineReply,
    kwargs: Py<PyDict>,
    injected_bg: Option<Py<PyAny>>,
    injected_resp: Option<Py<PyAny>>,
    timer: std::sync::Mutex<Option<Py<PyAny>>>,
    task: std::sync::Mutex<Option<Py<PyAny>>>,
    timeout_secs: Option<f64>,
}

#[pymethods]
impl InlineSend {
    /// `send(result, None)` on success, `send(None, exc)` on any raise
    /// (CancelledError from a timeout/disconnect cancel included — shipping
    /// the exception object gives byte-identical conversion to the classic
    /// path's re-raise → pyerr_to_response).
    fn __call__(&self, py: Python<'_>, obj: Py<PyAny>, exc: Py<PyAny>) {
        // Cancel a still-pending timeout timer. asyncio Handle.cancel() also
        // suppresses an already-queued-but-not-run callback, so after this the
        // timer can no longer race us (the Mutex-take below is the backstop).
        if let Ok(mut slot) = self.timer.lock() {
            if let Some(timer) = slot.take() {
                let _ = timer.call_method0(py, "cancel");
            }
        }
        let Some(tx) = self.reply.lock().ok().and_then(|mut g| g.take()) else {
            // Timed out (504 already delivered) or client disconnected —
            // drop everything. The runner already consumed the exception, so
            // no "exception was never retrieved" warning can occur.
            return;
        };
        if exc.is_none(py) {
            let _ = tx.send(LoopOutcome::Result {
                obj,
                kwargs: self.kwargs.clone_ref(py),
                injected_bg: self.injected_bg.as_ref().map(|o| o.clone_ref(py)),
                injected_resp: self.injected_resp.as_ref().map(|o| o.clone_ref(py)),
            });
        } else {
            let _ = tx.send(LoopOutcome::Error(PyErr::from_value(exc.bind(py).clone())));
        }
    }

    /// Timer callback: take the sender and deliver the TimeoutError NOW, then
    /// cancel the task. `pyerr_to_response` maps PyTimeoutError → 504
    /// text/plain "Gateway Timeout", NOT captured onto the app — the exact arm
    /// the classic `submit_fast` timeout takes.
    fn _timeout(&self, py: Python<'_>) {
        if let Some(tx) = self.reply.lock().ok().and_then(|mut g| g.take()) {
            let secs = self.timeout_secs.unwrap_or(f64::NAN);
            let _ = tx.send(LoopOutcome::Error(
                pyo3::exceptions::PyTimeoutError::new_err(format!(
                    "fastapi-turbo worker-loop submit timed out after {secs:?}s"
                )),
            ));
        }
        if let Ok(slot) = self.task.lock() {
            if let Some(task) = slot.as_ref() {
                let _ = task.call_method0(py, "cancel");
            }
        }
    }
}

/// Why an inline enqueue didn't happen.
enum InlineEnqueueError {
    /// Loop unavailable (closed / not yet cached) — parts returned intact so
    /// the caller can fall back to the classic needs-worker path.
    Recovered(Box<InlineParts>),
    /// Parts were consumed before the failure (allocation error) — unrecoverable.
    Lost(PyErr),
}

/// One short GIL tap on the tokio thread: wrap the parts in an `InlineJob` and
/// `call_soon_threadsafe` it onto the worker loop.
fn enqueue_inline_job(
    state: Arc<RouteState>,
    parts: Box<InlineParts>,
) -> Result<tokio::sync::oneshot::Receiver<LoopOutcome>, InlineEnqueueError> {
    let (tx, rx) = tokio::sync::oneshot::channel::<LoopOutcome>();
    let reply: InlineReply = Arc::new(std::sync::Mutex::new(Some(tx)));
    Python::attach(|py| {
        let Some(call_soon) = crate::handler_bridge::worker_call_soon() else {
            return Err(InlineEnqueueError::Recovered(parts));
        };
        let job = match Py::new(
            py,
            InlineJob {
                state,
                parts: Some(parts),
                reply,
            },
        ) {
            Ok(j) => j,
            Err(e) => return Err(InlineEnqueueError::Lost(e)),
        };
        match call_soon.call1(py, (job.bind(py),)) {
            Ok(_) => Ok(rx),
            // RuntimeError (loop closed) — take the parts back for the fallback.
            Err(e) => match job.borrow_mut(py).parts.take() {
                Some(p) => Err(InlineEnqueueError::Recovered(p)),
                None => Err(InlineEnqueueError::Lost(e)),
            },
        }
    })
}

/// Tokio-side epilogue: convert the loop's outcome into the HTTP response.
/// Runs on a tokio worker thread (post-await, possibly a different one than
/// enqueued) — per-request TLs are seeded HERE, inside one synchronous attach,
/// so the conversion helpers (`route_default_status` / `has_injected_response`
/// / `apply_injected_response` / `drain_background_tasks`) see them, and
/// `create_streaming_response`'s `spawn_blocking` has its runtime context.
fn finish_inline_outcome(
    state: &Arc<RouteState>,
    outcome: Result<LoopOutcome, tokio::sync::oneshot::error::RecvError>,
    range_header: Option<&str>,
    if_range_header: Option<&str>,
) -> Response {
    match outcome {
        // Sender dropped without sending (job dropped unrun — loop shutdown
        // mid-flight). Nothing to convert.
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            [("content-type", "text/plain; charset=utf-8")],
            "Internal Server Error",
        )
            .into_response(),
        Ok(LoopOutcome::Ready(resp)) => resp,
        Ok(LoopOutcome::Error(err)) => Python::attach(|py| pyerr_to_response(py, &err)),
        Ok(LoopOutcome::Result {
            obj,
            kwargs,
            injected_bg,
            injected_resp,
        }) => Python::attach(|py| {
            // No await between here and return — thread-stable, guard-safe.
            let _guard = DisconnectFlagGuard;
            ROUTE_DEFAULT_STATUS.with(|s| s.set(state.status_code));
            if let Some(r) = injected_resp {
                INJECTED_RESPONSE.with(|c| *c.borrow_mut() = Some(r));
            }
            if let Some(b) = injected_bg {
                INJECTED_BACKGROUND_TASKS.with(|c| *c.borrow_mut() = Some(b));
            }
            // Same gate as the classic arms (has_dep_params is always false here).
            if state.has_inject_background_tasks || state.has_dep_params {
                drain_background_tasks(py, kwargs.bind(py), &state.params);
            }
            let mut resp =
                py_to_response_with_request(py, obj.bind(py), range_header, if_range_header);
            apply_injected_response(py, &mut resp);
            resp
        }),
    }
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
    // axum's ``Query<HashMap<_,_>>`` extractor already parsed the query once;
    // this multimap is a SECOND parse, only needed for list-typed query params,
    // param-model raw_query, and dep list-query inputs. Skip it otherwise — the
    // empty map readers (`.get(...).unwrap_or_default()`) behave identically.
    let query_multi: HashMap<String, Vec<String>> = if state.needs_query_multi {
        let mut m: HashMap<String, Vec<String>> = HashMap::new();
        for (k, v) in url::form_urlencoded::parse(request.uri().query().unwrap_or("").as_bytes()) {
            m.entry(k.into_owned()).or_default().push(v.into_owned());
        }
        m
    } else {
        HashMap::new()
    };
    // In-process disconnect flag (streaming door) — read off the request's Axum
    // extension BEFORE the body is consumed; stashed in the thread-local below
    // (after the body await, on the dispatch thread) so the Request scope picks
    // it up. None for the socket path and for apps without is_disconnected.
    let disconnect_flag: Option<Py<PyAny>> = request
        .extensions()
        .get::<DisconnectFlag>()
        .map(|f| Python::attach(|py| f.0.clone_ref(py)));
    // === Pure Rust work — no GIL needed ===

    // For file/form params inspect Content-Type once. We support three body
    // shapes for these: `multipart/form-data`, `application/x-www-form-urlencoded`,
    // or plain JSON. Detection here is just reading the header value.
    #[derive(Copy, Clone, PartialEq, Eq)]
    enum FormKind {
        None,
        Multipart,
        UrlEncoded,
    }

    let (multipart_boundary, form_kind): (Option<String>, FormKind) =
        if state.has_file_params || state.has_form_params {
            if let Some(ct) = request
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
            {
                if let Some(b) = parse_boundary(ct) {
                    (Some(b), FormKind::Multipart)
                } else if ct
                    .get(.."application/x-www-form-urlencoded".len())
                    .is_some_and(|p| p.eq_ignore_ascii_case("application/x-www-form-urlencoded"))
                {
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

    // Capture the full header map only when something reads it:
    // - Header/Cookie params
    // - Request injection (vLLM/SGLang read request.headers)
    // - BaseHTTPMiddleware dispatch (Qwen auth reads authorization header)
    // - File/form params (read content-type / boundary off the clone)
    // - Body params (the body arm reads content-type off `headers`)
    // The clone is ~0.6-1μs for typical request headers; routes with none of
    // these (e.g. /hello, /path/{id}) skip it entirely.
    let needs_headers = state.has_header_params
        || state.has_inject_request
        || state.has_http_middleware
        || state.has_file_params
        || state.has_form_params
        // Body routes no longer force the clone — the body arm reads
        // Content-Type from the ``content_type`` carrier captured below.
        // A dependency input may read a Header/Cookie (or inject Request) that
        // isn't visible in the top-level ``route.params`` — keep the clone for
        // any dep route. (Conservative; the bench ``/with-deps`` dep reads a
        // header, so this is load-bearing.)
        || state.has_dep_params;
    let headers: Option<HeaderMap> = if needs_headers {
        Some(request.headers().clone())
    } else {
        None
    };

    // Capture Content-Type for the body arm. A ``HeaderValue`` clone is a
    // refcounted ``Bytes`` bump (no per-request heap alloc, unlike the old
    // ``to_string``) and lets a body-only route skip the full header clone
    // above: ``needs_headers`` drops ``has_body_params`` when the route has
    // no other header-reading param, so ``headers`` is ``None`` but the body
    // arm still sees the MIME via this carrier.
    let content_type: Option<axum::http::HeaderValue> = if state.has_body_params {
        request.headers().get("content-type").cloned()
    } else {
        None
    };

    // Capture `Range` + `If-Range` once so FileResponse can emit `206
    // Partial Content` (or bail to 200 when the client's validator is
    // stale per RFC 7233 §3.2) without re-reading the request at every
    // response-conversion site.
    let range_header: Option<String> = request
        .headers()
        .get("range")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let if_range_header: Option<String> = request
        .headers()
        .get("if-range")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());

    // Capture method/path/query ONLY when a consumer exists: the request-scope
    // ctxvar (exception handlers / Sentry), an http-middleware chain that inspects
    // request.url.path, or Request injection. For a plain route (the common case)
    // all three consumers are absent, so we skip 3 heap allocs + memcpys per
    // request. Every consumer already tolerates None (ctxvar early-returns;
    // metadata/inject run only under their flags), so None is safe here.
    let wants_scope_strings =
        state.wants_request_scope || state.has_http_middleware || state.has_inject_request;
    let scope_method = if wants_scope_strings {
        Some(request.method().as_str().to_string())
    } else {
        None
    };
    let scope_path = if wants_scope_strings {
        Some(request.uri().path().to_string())
    } else {
        None
    };
    let scope_query = if wants_scope_strings {
        Some(request.uri().query().unwrap_or("").to_string())
    } else {
        None
    };

    // Extract client address from ConnectInfo (set by into_make_service_with_connect_info).
    let client_addr: Option<SocketAddr> = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|ci| ci.0);

    // Only read body if we have body/file/form params — OR if the handler
    // injects `Request`, which vLLM uses to parse bodies manually via
    // `await request.body()` / `await request.json()`.
    // Also read when the handler wraps an HTTP middleware chain: custom
    // ASGI middlewares (body-size guards, signing checks) call
    // ``request.body()`` BEFORE the handler runs and the Python layer
    // needs the raw bytes in scope.
    let needs_body = state.has_body_params
        || state.has_file_params
        || state.has_form_params
        || state.has_inject_request
        || state.has_http_middleware;

    let (body_bytes, body_json, mut multipart_fields, raw_body_for_mw): (
        bytes::Bytes,
        Option<serde_json::Value>,
        Option<HashMap<String, Vec<ParsedField>>>,
        Option<bytes::Bytes>,
    ) = if needs_body {
        // Upper bound on the in-memory body buffer. The app's
        // ``max_request_size`` (set on ``FastAPI(...)``) is already
        // enforced by ``tower_http::limit::RequestBodyLimitLayer``
        // above this handler, returning 413 on oversized requests.
        // We keep a very large ceiling here (``usize::MAX``) so apps
        // that set ``max_request_size=50_000_000`` don't hit a hidden
        // 10 MiB cap inside the router. FastAPI/Starlette impose no
        // default limit — only what the user configures wins.
        let bb = match axum::body::to_bytes(request.into_body(), usize::MAX).await {
            Ok(b) => b,
            Err(e) => {
                return (StatusCode::BAD_REQUEST, format!("Failed to read body: {e}"))
                    .into_response();
            }
        };

        // Preserve the raw body bytes for the middleware chain regardless
        // of whether we also parse them below — multipart/urlencoded
        // parsing otherwise drops the original bytes.
        let mw_raw = if state.has_http_middleware {
            Some(bb.clone())
        } else {
            None
        };

        // Multipart path: parse into named fields
        if let Some(ref boundary) = multipart_boundary {
            match parse_multipart(bb.clone(), boundary).await {
                Ok(fields) => (bytes::Bytes::new(), None, Some(fields), mw_raw),
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
                fields.entry(k.to_string()).or_default().push(ParsedField {
                    name: k.to_string(),
                    filename: None,
                    content_type: None,
                    data: bytes::Bytes::from(v.into_owned().into_bytes()),
                    headers: Vec::new(),
                });
            }
            (bytes::Bytes::new(), None, Some(fields), mw_raw)
        } else {
            // JSON / raw bytes body path (existing behavior)
            let all_have_models = state
                .params
                .iter()
                .filter(|p| p.kind == "body")
                .all(|p| p.cached_validator.is_some() || p.model_class.is_some());
            let json = if all_have_models || bb.is_empty() {
                None
            } else {
                serde_json::from_slice(&bb).ok()
            };
            (bb, json, None, mw_raw)
        }
    } else {
        drop(request);
        (bytes::Bytes::new(), None, None, None)
    };

    let path_map = path_params.map(|Path(m)| m).unwrap_or_default();

    // === FASTAPI_TURBO_ASYNC_INLINE (E): async request runs ENTIRELY on the
    // persistent worker loop; this tokio task awaits a oneshot instead of
    // blocking an OS thread on a threading.Event. Dispatched BEFORE the
    // thread-local guard/status set below — the oneshot await may resume this
    // future on a DIFFERENT tokio worker thread, so no pre-await TLs are
    // allowed on this path (finish_inline_outcome seeds its own, post-await).
    if state.is_async
        && !state.has_dep_params
        && async_inline_enabled()
        && !state.has_http_middleware
        && !state.has_file_params
        && !state.has_form_params
        && state
            .handler_async_class
            .load(std::sync::atomic::Ordering::Relaxed)
            == crate::handler_bridge::ASYNC_CLASS_NEEDS_WORKER
    {
        // A route only classifies NEEDS_WORKER via the classic path, which
        // already initialized the worker — defensive init all the same.
        crate::handler_bridge::init_async_worker();
        let parts = Box::new(InlineParts {
            path_map,
            query_params,
            query_multi,
            headers,
            content_type,
            body_bytes,
            body_json,
            scope_method,
            scope_path,
            scope_query,
            client_addr,
            disconnect_flag,
        });
        match enqueue_inline_job(state.clone(), parts) {
            Ok(rx) => {
                let outcome = rx.await;
                return finish_inline_outcome(
                    &state,
                    outcome,
                    range_header.as_deref(),
                    if_range_header.as_deref(),
                );
            }
            Err(InlineEnqueueError::Lost(e)) => {
                return Python::attach(|py| pyerr_to_response(py, &e));
            }
            Err(InlineEnqueueError::Recovered(p)) => {
                // Loop unavailable (closed) — classic needs-worker dispatch with
                // the recovered parts. Mirrors the block below for the gated
                // subset (no deps, no http-middleware, no file/form).
                let InlineParts {
                    path_map,
                    query_params,
                    query_multi,
                    headers,
                    content_type,
                    body_bytes,
                    body_json,
                    scope_method,
                    scope_path,
                    scope_query,
                    client_addr,
                    disconnect_flag,
                } = *p;
                let _disc_guard = DisconnectFlagGuard;
                if let Some(flag) = disconnect_flag {
                    REQUEST_DISCONNECT_FLAG.with(|f| *f.borrow_mut() = Some(flag));
                }
                ROUTE_DEFAULT_STATUS.with(|s| s.set(state.status_code));
                return tokio::task::block_in_place(|| {
                    Python::attach(|py| {
                        set_request_scope_ctxvar(
                            py,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &state,
                        );
                        let body_json_opt = if state.has_body_params {
                            body_json.as_ref()
                        } else {
                            None
                        };
                        let mut multipart_fields: Option<HashMap<String, Vec<ParsedField>>> = None;
                        let kwargs = match extract_params_to_pydict_full(
                            py,
                            &state.params,
                            &path_map,
                            &query_params,
                            &query_multi,
                            &headers,
                            content_type.as_ref().and_then(|v| v.to_str().ok()),
                            &body_json_opt,
                            &body_bytes,
                            &mut multipart_fields,
                            state.defers_extraction_errors,
                            state.lax_content_type,
                            state.has_param_model,
                        ) {
                            Ok(kw) => kw,
                            Err(resp) => return resp,
                        };
                        if let Err(e) = inject_framework_objects(
                            py,
                            &kwargs,
                            &state,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &headers,
                            &path_map,
                            &query_params,
                            &body_bytes,
                            &client_addr,
                        ) {
                            return pyerr_to_response(py, &e);
                        }
                        match crate::handler_bridge::call_async_on_local_loop_classified(
                            py,
                            &state.handler,
                            &kwargs,
                            &state.handler_async_class,
                            state.worker_timeout,
                        ) {
                            Ok(r) => {
                                if state.has_inject_background_tasks || state.has_dep_params {
                                    drain_background_tasks(py, &kwargs, &state.params);
                                }
                                let mut resp = py_to_response_with_request(
                                    py,
                                    r.bind(py),
                                    range_header.as_deref(),
                                    if_range_header.as_deref(),
                                );
                                apply_injected_response(py, &mut resp);
                                resp
                            }
                            Err(e) => pyerr_to_response(py, &e),
                        }
                    })
                });
            }
        }
    }

    // Publish the disconnect flag to the per-request thread-local NOW — after the
    // body await, so we're on the same worker thread the dispatch (and the
    // Request-scope build) runs on. The guard clears it when handle_request
    // returns, so it never leaks to the next request on this thread.
    let _disc_guard = DisconnectFlagGuard;
    if let Some(flag) = disconnect_flag {
        REQUEST_DISCONNECT_FLAG.with(|f| *f.borrow_mut() = Some(flag));
    }
    // Route-level default status (``status_code=201``) for py_to_response to apply
    // to non-Response handler results; the guard clears it after the request.
    ROUTE_DEFAULT_STATUS.with(|s| s.set(state.status_code));

    // === Fast path: sync handler with NO dependencies ===
    // Do everything in a SINGLE block_in_place → with_gil (1 GIL acquisition, no thread hop)
    if !state.is_async && !state.has_dep_params {
        if !state.has_any_params {
            if state.has_http_middleware {
                // Middleware wrapper needs metadata kwargs
                return Python::attach(|py| {
                    set_request_scope_ctxvar(py, &scope_method, &scope_path, &scope_query, &state);
                    let kwargs = PyDict::new(py);
                    if state.has_http_middleware {
                        inject_request_metadata(
                            py,
                            &kwargs,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &headers,
                        );
                        if let Some(ref raw) = raw_body_for_mw {
                            if !raw.is_empty() {
                                let _ = kwargs.set_item(
                                    "__fastapi_turbo_raw_body_bytes__",
                                    pyo3::types::PyBytes::new(py, raw),
                                );
                            }
                        }
                    }
                    match state.handler.call(py, (), Some(&kwargs)) {
                        Ok(py_result) => py_to_response_with_request(
                            py,
                            py_result.bind(py),
                            range_header.as_deref(),
                            if_range_header.as_deref(),
                        ),
                        Err(py_err) => pyerr_to_response(py, &py_err),
                    }
                });
            }
            // Ultra-fast path: zero-param, no middleware
            return Python::attach(|py| {
                set_request_scope_ctxvar(py, &scope_method, &scope_path, &scope_query, &state);
                match state.handler.call0(py) {
                    Ok(py_result) => py_to_response_with_request(
                        py,
                        py_result.bind(py),
                        range_header.as_deref(),
                        if_range_header.as_deref(),
                    ),
                    Err(py_err) => pyerr_to_response(py, &py_err),
                }
            });
        }

        // Sync handler with params — direct GIL attach on the tokio worker,
        // matching the zero-param ultra-fast path above.
        //
        // This arm deliberately does NOT use ``tokio::task::block_in_place``
        // anymore. Measured with per-phase timers (20k-request medians,
        // conn=1): block_in_place cost 1.3μs at entry + 1.4μs at exit on
        // EVERY body/path-param request — the largest single non-work item
        // in the PUT/POST hot path (wire p50 33μs → 30μs without it, and
        // c8 throughput 63k → 76k rps, c64 p99 3.9ms → 1.7ms).
        //
        // The traded-away property: block_in_place hands the worker's core
        // to a replacement thread, so sync handlers that block WITHOUT the
        // GIL (DB drivers, file IO, time.sleep) could overlap beyond the
        // worker count. Direct attach caps that overlap at the tokio worker
        // count per process (measured: 5ms-sleep handler at c64 = ~10.4k rps
        // with block_in_place vs ~2.9k rps capped). We take the cap because:
        //   * GIL-bound handlers (the common case) serialize identically
        //     either way — the cap only binds for GIL-releasing handlers
        //     held longer than a few ms at concurrency > n_workers.
        //   * block_in_place's replacement-worker spawn was catastrophically
        //     fragile under exactly that load shape: a cold-start burst of
        //     64 concurrent 5ms GIL-releasing handlers wedged the whole
        //     server PERMANENTLY (all threads parked in take_gil, 12 rps
        //     then zero; reproduced 2/3 attempts). The zero-param path never
        //     wedges, and neither does this arm now.
        //   * Recommended deployments run FASTAPI_TURBO_WORKERS processes;
        //     blocking overlap scales with processes × tokio workers.
        return Python::attach(|py| {
            set_request_scope_ctxvar(py, &scope_method, &scope_path, &scope_query, &state);
            let body_json_opt = if state.has_body_params {
                body_json.as_ref()
            } else {
                None
            };
            let kwargs = match extract_params_to_pydict_full(
                py,
                &state.params,
                &path_map,
                &query_params,
                &query_multi,
                &headers,
                content_type.as_ref().and_then(|v| v.to_str().ok()),
                &body_json_opt,
                &body_bytes,
                &mut multipart_fields,
                state.defers_extraction_errors,
                state.lax_content_type,
                state.has_param_model,
            ) {
                Ok(kw) => kw,
                Err(resp) => return resp,
            };
            if let Err(e) = inject_framework_objects(
                py,
                &kwargs,
                &state,
                &scope_method,
                &scope_path,
                &scope_query,
                &headers,
                &path_map,
                &query_params,
                &body_bytes,
                &client_addr,
            ) {
                return pyerr_to_response(py, &e);
            }
            if state.has_http_middleware {
                inject_request_metadata(
                    py,
                    &kwargs,
                    &scope_method,
                    &scope_path,
                    &scope_query,
                    &headers,
                );
                // Seed the middleware Request's ``_body`` cache with
                // the raw (pre-multipart-parse) bytes.
                if let Some(ref raw) = raw_body_for_mw {
                    if !raw.is_empty() {
                        let _ = kwargs.set_item(
                            "__fastapi_turbo_raw_body_bytes__",
                            pyo3::types::PyBytes::new(py, raw),
                        );
                    }
                }
            }
            match state.handler.call(py, (), Some(&kwargs)) {
                Ok(py_result) => {
                    // Gated: no BackgroundTasks param and no deps (a dep can share the
                    // per-request instance) means there is provably nothing to drain.
                    if state.has_inject_background_tasks || state.has_dep_params {
                        drain_background_tasks(py, &kwargs, &state.params);
                    }
                    let mut resp = py_to_response_with_request(
                        py,
                        py_result.bind(py),
                        range_header.as_deref(),
                        if_range_header.as_deref(),
                    );
                    apply_injected_response(py, &mut resp);
                    resp
                }
                Err(py_err) => pyerr_to_response(py, &py_err),
            }
        });
    }

    // === Async fast path: run on per-thread event loop (Granian pattern) ===
    // For async handlers (with or without deps), run via loop.run_until_complete()
    // on a thread-local event loop. This eliminates the ~100-150μs cross-thread
    // overhead of run_coroutine_threadsafe. All DB awaits resolve on THIS thread.
    // Async handlers WITHOUT dependencies use the dedicated local-loop path here.
    // Async handlers WITH dependencies fall through to the unified deps loop below,
    // which resolves the dependency graph AND drives the async handler — block C's
    // extract-only path never resolved deps.
    if state.is_async && !state.has_dep_params {
        return tokio::task::block_in_place(|| {
            Python::attach(|py| {
                set_request_scope_ctxvar(py, &scope_method, &scope_path, &scope_query, &state);
                // Build kwargs from params
                let body_json_opt = if state.has_body_params {
                    body_json.as_ref()
                } else {
                    None
                };

                if !state.has_any_params {
                    let kwargs = PyDict::new(py);
                    if state.has_http_middleware {
                        inject_request_metadata(
                            py,
                            &kwargs,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &headers,
                        );
                        if let Some(ref raw) = raw_body_for_mw {
                            if !raw.is_empty() {
                                let _ = kwargs.set_item(
                                    "__fastapi_turbo_raw_body_bytes__",
                                    pyo3::types::PyBytes::new(py, raw),
                                );
                            }
                        }
                    }
                    match crate::handler_bridge::call_async_on_local_loop_classified(
                        py,
                        &state.handler,
                        &kwargs,
                        &state.handler_async_class,
                        state.worker_timeout,
                    ) {
                        Ok(r) => py_to_response_with_request(
                            py,
                            r.bind(py),
                            range_header.as_deref(),
                            if_range_header.as_deref(),
                        ),
                        Err(e) => pyerr_to_response(py, &e),
                    }
                } else if !state.has_dep_params {
                    let kwargs = match extract_params_to_pydict_full(
                        py,
                        &state.params,
                        &path_map,
                        &query_params,
                        &query_multi,
                        &headers,
                        content_type.as_ref().and_then(|v| v.to_str().ok()),
                        &body_json_opt,
                        &body_bytes,
                        &mut multipart_fields,
                        state.defers_extraction_errors,
                        state.lax_content_type,
                        state.has_param_model,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py,
                        &kwargs,
                        &state,
                        &scope_method,
                        &scope_path,
                        &scope_query,
                        &headers,
                        &path_map,
                        &query_params,
                        &body_bytes,
                        &client_addr,
                    ) {
                        return pyerr_to_response(py, &e);
                    }
                    if state.has_http_middleware {
                        inject_request_metadata(
                            py,
                            &kwargs,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &headers,
                        );
                        if let Some(ref raw) = raw_body_for_mw {
                            if !raw.is_empty() {
                                let _ = kwargs.set_item(
                                    "__fastapi_turbo_raw_body_bytes__",
                                    pyo3::types::PyBytes::new(py, raw),
                                );
                            }
                        }
                    }
                    match crate::handler_bridge::call_async_on_local_loop_classified(
                        py,
                        &state.handler,
                        &kwargs,
                        &state.handler_async_class,
                        state.worker_timeout,
                    ) {
                        Ok(r) => {
                            // Gated: no BackgroundTasks param and no deps (a dep can share the
                            // per-request instance) means there is provably nothing to drain.
                            if state.has_inject_background_tasks || state.has_dep_params {
                                drain_background_tasks(py, &kwargs, &state.params);
                            }
                            let mut resp = py_to_response_with_request(
                                py,
                                r.bind(py),
                                range_header.as_deref(),
                                if_range_header.as_deref(),
                            );
                            apply_injected_response(py, &mut resp);
                            resp
                        }
                        Err(e) => pyerr_to_response(py, &e),
                    }
                } else {
                    let kwargs = match extract_params_to_pydict_full(
                        py,
                        &state.params,
                        &path_map,
                        &query_params,
                        &query_multi,
                        &headers,
                        content_type.as_ref().and_then(|v| v.to_str().ok()),
                        &body_json_opt,
                        &body_bytes,
                        &mut multipart_fields,
                        state.defers_extraction_errors,
                        state.lax_content_type,
                        state.has_param_model,
                    ) {
                        Ok(kw) => kw,
                        Err(resp) => return resp,
                    };
                    if let Err(e) = inject_framework_objects(
                        py,
                        &kwargs,
                        &state,
                        &scope_method,
                        &scope_path,
                        &scope_query,
                        &headers,
                        &path_map,
                        &query_params,
                        &body_bytes,
                        &client_addr,
                    ) {
                        return pyerr_to_response(py, &e);
                    }
                    if state.has_http_middleware {
                        inject_request_metadata(
                            py,
                            &kwargs,
                            &scope_method,
                            &scope_path,
                            &scope_query,
                            &headers,
                        );
                        if let Some(ref raw) = raw_body_for_mw {
                            if !raw.is_empty() {
                                let _ = kwargs.set_item(
                                    "__fastapi_turbo_raw_body_bytes__",
                                    pyo3::types::PyBytes::new(py, raw),
                                );
                            }
                        }
                    }
                    // ASYNC handler WITH dependencies — must be DRIVEN, not just
                    // called (a bare .call returns the un-awaited coroutine). We're
                    // inside `if state.is_async`, so always drive on the local loop,
                    // exactly like the no-dep async branches above.
                    match crate::handler_bridge::call_async_on_local_loop_classified(
                        py,
                        &state.handler,
                        &kwargs,
                        &state.handler_async_class,
                        state.worker_timeout,
                    ) {
                        Ok(r) => {
                            // Gated: no BackgroundTasks param and no deps (a dep can share the
                            // per-request instance) means there is provably nothing to drain.
                            if state.has_inject_background_tasks || state.has_dep_params {
                                drain_background_tasks(py, &kwargs, &state.params);
                            }
                            let mut resp = py_to_response_with_request(
                                py,
                                r.bind(py),
                                range_header.as_deref(),
                                if_range_header.as_deref(),
                            );
                            apply_injected_response(py, &mut resp);
                            resp
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
            set_request_scope_ctxvar(py, &scope_method, &scope_path, &scope_query, &state);
            let mut resolved: HashMap<String, Py<PyAny>> = HashMap::new();
            let mut dep_cache: HashMap<u64, String> = HashMap::new();
            // Live ``yield`` dependency generators awaiting teardown.
            // (generator, is_function_scope). Function-scope deps tear down before
            // the response; request-scope (default) after the body.
            let mut gen_deps: Vec<(Py<PyAny>, bool)> = Vec::new();
            // Accumulate missing/coercion errors from extra-dep INPUT params so
            // a route with several ``Depends`` each missing a required param
            // surfaces ALL of them in one 422 (FA parity), and skip any dep
            // whose own inputs failed to extract.
            let mut dep_extraction_errors: Vec<serde_json::Value> = Vec::new();
            let mut failed_sources: std::collections::HashSet<String> =
                std::collections::HashSet::new();
            for param in &state.params {
                match param.kind.as_str() {
                    "dependency" => {
                        // Skip a dep whose input(s) failed extraction — its
                        // error is already accumulated; calling it would raise
                        // on the missing kwarg and mask the real 422.
                        if param
                            .dep_input_names
                            .iter()
                            .any(|(_, src)| failed_sources.contains(src))
                        {
                            continue;
                        }
                        // Check cache first
                        if let Some(func_id) = param.dep_callable_id {
                            if let Some(cached_key) = dep_cache.get(&func_id) {
                                if let Some(cached_val) = resolved.get(cached_key) {
                                    resolved.insert(param.name.clone(), cached_val.clone_ref(py));
                                    continue;
                                }
                            }
                        }

                        let Some(ref dep_callable) = param.dep_callable else {
                            continue;
                        };

                        // Build kwargs for this dep from previously resolved values
                        let dep_kwargs = PyDict::new(py);
                        for (param_name, source_key) in &param.dep_input_names {
                            if let Some(val) = resolved.get(source_key) {
                                let _ = dep_kwargs.set_item(param_name, val.bind(py));
                            }
                        }

                        // Call the dep. A ``yield`` dep is entered (and stashed for
                        // teardown); async via send(None); plain sync directly.
                        let result = if param.is_generator_dep {
                            enter_sync_generator_dep(
                                py,
                                dep_callable,
                                &dep_kwargs,
                                &mut gen_deps,
                                param.is_function_scope,
                            )
                        } else if param.is_async_dep {
                            // Try-sync first; a suspending coroutine routes to the
                            // shared worker loop so deps that genuinely ``await``
                            // resolve instead of erroring. The route-build-resolved
                            // timeout keeps loop-affinity / configured timeouts.
                            crate::handler_bridge::call_async_on_local_loop_classified(
                                py,
                                dep_callable,
                                &dep_kwargs,
                                &param.dep_async_class,
                                state.worker_timeout,
                            )
                        } else {
                            dep_callable.call(py, (), Some(&dep_kwargs))
                        };

                        match result {
                            Ok(val) => {
                                if let Some(func_id) = param.dep_callable_id {
                                    dep_cache.insert(func_id, param.name.clone());
                                }
                                resolved.insert(param.name.clone(), val);
                            }
                            Err(py_err) => {
                                teardown_generator_deps(py, &gen_deps, true);
                                if let Some(resp) = try_user_dep_exception_handler(py, &py_err) {
                                    return resp;
                                }
                                return pyerr_to_response(py, &py_err);
                            }
                        }
                    }
                    _ => {
                        // Only extract dependency-INPUT params here (into `resolved`
                        // for dep wiring). Handler-facing params — incl. form/file and
                        // body — are produced by the full extractor below so the deps
                        // path reaches parity with the no-dep fast paths.
                        if !param.is_handler_param {
                            if param.kind.starts_with("inject_") {
                                // A dependency that takes a Request/Response/etc. —
                                // build the framework object into `resolved` so the
                                // dep wiring can feed it as an input.
                                match build_injected_object(
                                    py,
                                    param.kind.as_str(),
                                    &state,
                                    &scope_method,
                                    &scope_path,
                                    &scope_query,
                                    &headers,
                                    &path_map,
                                    &query_params,
                                    &body_bytes,
                                    &client_addr,
                                    &param.oauth_scopes,
                                ) {
                                    Ok(obj) => {
                                        resolved.insert(param.name.clone(), obj);
                                    }
                                    Err(e) => {
                                        teardown_generator_deps(py, &gen_deps, true);
                                        if let Some(resp) = try_user_dep_exception_handler(py, &e) {
                                            return resp;
                                        }
                                        return pyerr_to_response(py, &e);
                                    }
                                }
                            } else {
                                let before = dep_extraction_errors.len();
                                if let Err(resp) = extract_single_param(
                                    py,
                                    param,
                                    &path_map,
                                    &query_params,
                                    &query_multi,
                                    &headers,
                                    &body_json,
                                    &body_bytes,
                                    &mut multipart_fields,
                                    &mut resolved,
                                    &mut dep_extraction_errors,
                                ) {
                                    // Body-level error short-circuits with a
                                    // complete combined 422.
                                    teardown_generator_deps(py, &gen_deps, true);
                                    return resp;
                                }
                                // A scalar that failed extraction was pushed to
                                // the accumulator (not ``resolved``) — mark it so
                                // dependent deps skip rather than raise.
                                if dep_extraction_errors.len() > before {
                                    failed_sources.insert(param.name.clone());
                                }
                            }
                        }
                    }
                }
            }

            // Extra-dep input params that were missing/invalid surface as one
            // combined 422 across ALL deps (FA accumulates them) before the
            // handler-param extractor runs.
            if !dep_extraction_errors.is_empty() {
                teardown_generator_deps(py, &gen_deps, true);
                return dispatch_validation_error(serde_json::json!({
                    "detail": dep_extraction_errors,
                }));
            }

            // Build handler kwargs via the full extractor (scalars/body/form/file,
            // skipping deps + dep-inputs through is_handler_param), then overlay the
            // resolved dependency results and inject framework objects. This brings
            // the deps path to parity with the no-dep fast paths for special params
            // (Request/Response/BackgroundTasks/SecurityScopes) and Form/File.
            let body_json_opt = if state.has_body_params {
                body_json.as_ref()
            } else {
                None
            };
            let kwargs = match extract_params_to_pydict_full(
                py,
                &state.params,
                &path_map,
                &query_params,
                &query_multi,
                &headers,
                content_type.as_ref().and_then(|v| v.to_str().ok()),
                &body_json_opt,
                &body_bytes,
                &mut multipart_fields,
                state.defers_extraction_errors,
                state.lax_content_type,
                state.has_param_model,
            ) {
                Ok(kw) => kw,
                Err(resp) => {
                    teardown_generator_deps(py, &gen_deps, true);
                    return resp;
                }
            };
            for param in &state.params {
                if param.is_handler_param && param.kind == "dependency" {
                    if let Some(val) = resolved.get(&param.name) {
                        let _ = kwargs.set_item(param.name_pystr(py), val.bind(py));
                    }
                }
            }
            if let Err(e) = inject_framework_objects(
                py,
                &kwargs,
                &state,
                &scope_method,
                &scope_path,
                &scope_query,
                &headers,
                &path_map,
                &query_params,
                &body_bytes,
                &client_addr,
            ) {
                teardown_generator_deps(py, &gen_deps, true);
                return pyerr_to_response(py, &e);
            }
            if state.has_http_middleware {
                inject_request_metadata(
                    py,
                    &kwargs,
                    &scope_method,
                    &scope_path,
                    &scope_query,
                    &headers,
                );
                if let Some(ref raw) = raw_body_for_mw {
                    if !raw.is_empty() {
                        let _ = kwargs.set_item(
                            "__fastapi_turbo_raw_body_bytes__",
                            pyo3::types::PyBytes::new(py, raw),
                        );
                    }
                }
            }

            // Call handler. Deps are already resolved into `kwargs` above, so an
            // async handler is driven on the local loop (handles suspension) — the
            // 599 fallback below can't re-resolve deps, so we must not rely on it.
            let result = if state.is_async {
                crate::handler_bridge::call_async_on_local_loop_classified(
                    py,
                    &state.handler,
                    &kwargs,
                    &state.handler_async_class,
                    state.worker_timeout,
                )
            } else {
                state.handler.call(py, (), Some(&kwargs))
            };

            match result {
                Ok(py_result) => {
                    // Run any BackgroundTasks the handler received (deferred).
                    // Gated: no BackgroundTasks param and no deps (a dep can share the
                    // per-request instance) means there is provably nothing to drain.
                    if state.has_inject_background_tasks || state.has_dep_params {
                        drain_background_tasks(py, &kwargs, &state.params);
                    }
                    // FA exit-stack order: FUNCTION-scope yield-deps (inner stack)
                    // tear down BEFORE the response is built/sent — a post-yield
                    // raise becomes the response.
                    if let Err(e) = teardown_function_scope_gens(py, &gen_deps) {
                        // Don't leak request-scope deps — close them, then render
                        // the function-scope raise through the user handlers.
                        teardown_request_scope_gens(py, &gen_deps, true);
                        try_user_dep_exception_handler(py, &e)
                            .unwrap_or_else(|| pyerr_to_response(py, &e))
                    } else {
                        // A STREAMING body that reads a request-scope dep must keep
                        // it open until end-of-stream — wrap body_iterator so the
                        // deps tear down after the body (returns true → skip the
                        // immediate teardown). Must run BEFORE py_to_response reads
                        // the (now-wrapped) body_iterator.
                        let deferred =
                            maybe_defer_request_scope_to_stream(py, py_result.bind(py), &gen_deps);
                        // Build the response while request-scope deps are still open
                        // (lazy ORM rows materialize) — matches FA's exit-stack order.
                        let mut resp = py_to_response_with_request(
                            py,
                            py_result.bind(py),
                            range_header.as_deref(),
                            if_range_header.as_deref(),
                        );
                        apply_injected_response(py, &mut resp);
                        if !deferred {
                            // REQUEST-scope (default) yield-deps tear down AFTER the
                            // (buffered) response.
                            teardown_request_scope_gens(py, &gen_deps, false);
                        }
                        resp
                    }
                }
                Err(ref py_err) => {
                    // FA exit-stack parity: throw the handler error into the
                    // yield-deps; a dep that swallows it surfaces FastAPIError.
                    // (Async handlers always resolve via call_async_on_local_loop_classified,
                    // which routes a suspending coroutine to the worker loop — it never
                    // surfaces a "needs event loop" error, so the old 599 fallback that
                    // used to live here was unreachable and has been removed.)
                    let final_err =
                        teardown_generator_deps_error(py, &gen_deps, py_err.clone_ref(py));
                    pyerr_to_response(py, &final_err)
                }
            }
        })
    });

    resp
}

// ── yield (generator) dependency helpers ─────────────────────────────

/// Enter a sync ``yield`` dependency: call it to get the generator, advance to
/// the first yield (``send(None)``) to obtain the dependency value, and stash the
/// live generator so its teardown runs after the response is built.
fn enter_sync_generator_dep(
    py: Python<'_>,
    dep_callable: &Py<PyAny>,
    dep_kwargs: &pyo3::Bound<'_, PyDict>,
    gen_deps: &mut Vec<(Py<PyAny>, bool)>,
    is_function_scope: bool,
) -> PyResult<Py<PyAny>> {
    let gen = dep_callable.call(py, (), Some(dep_kwargs))?;
    match gen.call_method1(py, "send", (py.None(),)) {
        Ok(v) => {
            gen_deps.push((gen, is_function_scope));
            Ok(v)
        }
        // A generator that returns without yielding has no value and nothing to
        // tear down — surface its StopIteration ``value`` (usually None).
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => Ok(e
            .value(py)
            .getattr("value")
            .map(|v| v.unbind())
            .unwrap_or_else(|_| py.None())),
        Err(e) => Err(e),
    }
}

/// Run teardown for every entered ``yield`` dependency, in reverse order. On the
/// success path we advance past the yield (running post-yield cleanup, e.g.
/// ``db.close()``); on the error path we ``close()`` the generator (raising
/// ``GeneratorExit`` so ``try/finally`` cleanup still runs). Teardown errors are
/// swallowed — the response has already been produced.
fn teardown_generator_deps(py: Python<'_>, gen_deps: &[(Py<PyAny>, bool)], errored: bool) {
    for (gen, _is_func) in gen_deps.iter().rev() {
        if errored {
            let _ = gen.call_method0(py, "close");
        } else if gen.call_method1(py, "send", (py.None(),)).is_ok() {
            // Yielded again (multi-yield dep) — close out the remainder.
            // (StopIteration / teardown error means it's already done.)
            let _ = gen.call_method0(py, "close");
        }
    }
}

/// Tear down FUNCTION-scope yield deps (FA ``function_stack``) on the success
/// path — BEFORE the response is built/sent. Advances each past its yield (LIFO);
/// a post-yield raise is RETURNED so the caller turns it into the response (FA's
/// ``function_stack.__aexit__`` propagating before ``await response()``).
fn teardown_function_scope_gens(py: Python<'_>, gen_deps: &[(Py<PyAny>, bool)]) -> PyResult<()> {
    for (gen, is_func) in gen_deps.iter().rev() {
        if !*is_func {
            continue;
        }
        match gen.call_method1(py, "send", (py.None(),)) {
            // Yielded again (multi-yield) — close out the remainder.
            Ok(_) => {
                let _ = gen.call_method0(py, "close");
            }
            // Normal completion.
            Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {}
            // Raised after yield → becomes the response.
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// Tear down REQUEST-scope (default) yield deps (FA ``request_stack``) — AFTER the
/// response body. ``errored`` closes them (GeneratorExit); otherwise advances past
/// the yield. A post-yield raise is CAPTURED onto the app (the response is already
/// sent, so TestClient re-raises it) rather than swallowed.
fn teardown_request_scope_gens(py: Python<'_>, gen_deps: &[(Py<PyAny>, bool)], errored: bool) {
    for (gen, is_func) in gen_deps.iter().rev() {
        if *is_func {
            continue;
        }
        if errored {
            let _ = gen.call_method0(py, "close");
            continue;
        }
        match gen.call_method1(py, "send", (py.None(),)) {
            Ok(_) => {
                let _ = gen.call_method0(py, "close");
            }
            Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {}
            Err(e) => {
                if let Some(app) = current_app(py) {
                    if let Ok(lst) = app.getattr(py, "_captured_server_exceptions") {
                        let _ = lst.call_method1(py, "append", (e.value(py),));
                    }
                }
            }
        }
    }
}

/// For a STREAMING result with REQUEST-scope yield deps, defer their teardown to
/// end-of-stream: wrap the ``StreamingResponse``'s ``body_iterator`` (Python helper)
/// so the deps stay open while the body is read (FA ``request_stack`` order — the
/// session a streaming body iterates must not be closed first). Returns true when
/// it deferred, so the caller SKIPS the immediate request-scope teardown.
fn maybe_defer_request_scope_to_stream(
    py: Python<'_>,
    result: &Bound<'_, PyAny>,
    gen_deps: &[(Py<PyAny>, bool)],
) -> bool {
    // Only StreamingResponse-like results carry a body_iterator.
    if !result.hasattr("body_iterator").unwrap_or(false) {
        return false;
    }
    let req_gens = pyo3::types::PyList::empty(py);
    for (gen, is_func) in gen_deps.iter() {
        if !*is_func {
            let _ = req_gens.append(gen.bind(py));
        }
    }
    if req_gens.is_empty() {
        return false;
    }
    let app_arg = match current_app(py) {
        Some(a) => a.into_bound(py),
        None => py.None().into_bound(py),
    };
    py.import("fastapi_turbo.applications")
        .and_then(|m| m.getattr("_door_wrap_stream_teardown"))
        .and_then(|f| f.call1((app_arg, result, &req_gens)))
        .is_ok()
}

/// Door dep-resolution error path: route a dependency-raised exception through
/// the app's user ``@app.exception_handler`` handlers (FA parity — the Python
/// dispatcher does this; the door previously rendered the default 500 directly,
/// ignoring user handlers for exceptions raised INSIDE a dependency). Returns
/// the handler's response when one matched, else ``None`` (caller falls back to
/// ``pyerr_to_response``, which renders the default + captures for re-raise).
fn try_user_dep_exception_handler(py: Python<'_>, py_err: &PyErr) -> Option<Response> {
    let app = current_app(py)?;
    let exc = py_err.value(py);
    let result = app
        .bind(py)
        .call_method1("_door_handle_dep_exception", (exc,))
        .ok()?;
    if result.is_none() {
        return None;
    }
    Some(py_to_response_with_request(py, &result, None, None))
}

/// Construct a ``fastapi_turbo.exceptions.FastAPIError`` (re-exports the real
/// ``fastapi.exceptions.FastAPIError``). Falls back to the import/construction
/// error so the caller always gets *some* PyErr to surface.
fn fastapi_error(py: Python<'_>, msg: &str) -> PyErr {
    match py
        .import("fastapi_turbo.exceptions")
        .and_then(|m| m.getattr("FastAPIError"))
        .and_then(|cls| cls.call1((msg,)))
    {
        Ok(inst) => PyErr::from_value(inst),
        Err(e) => e,
    }
}

/// Error-path teardown that mirrors FastAPI's exit-stack protocol: the live
/// exception is thrown into each ``yield`` dependency (reverse / LIFO order).
/// A dependency that catches the exception WITHOUT re-raising (its generator
/// returns via ``StopIteration``) *suppresses* it — FA forbids this and raises
/// ``FastAPIError``. A dependency that re-raises (the same or a different
/// exception) propagates it. Returns the error to surface to the client.
fn teardown_generator_deps_error(
    py: Python<'_>,
    gen_deps: &[(Py<PyAny>, bool)],
    original: PyErr,
) -> PyErr {
    let mut live: Option<PyErr> = Some(original);
    for (gen, _is_func) in gen_deps.iter().rev() {
        match live.take() {
            Some(err) => {
                let exc = err.value(py).clone();
                match gen.call_method1(py, "throw", (exc,)) {
                    // Generator returned without re-raising → swallowed the error.
                    Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                        live = None;
                    }
                    // Generator re-raised (same or different exception) → propagate.
                    Err(e) => {
                        live = Some(e);
                    }
                    // Generator yielded again after throw — a misbehaving dep;
                    // close it and keep the original error live.
                    Ok(_) => {
                        let _ = gen.call_method0(py, "close");
                        live = Some(err);
                    }
                }
            }
            None => {
                // Exception already suppressed by an inner dep — this outer dep
                // exits normally (advance past its yield, then close).
                if gen.call_method1(py, "send", (py.None(),)).is_ok() {
                    let _ = gen.call_method0(py, "close");
                }
            }
        }
    }
    match live {
        Some(e) => e,
        // The exception was swallowed by a yield-dep that didn't re-raise.
        None => fastapi_error(
            py,
            "Response not awaited. There's a high chance that the \
             application code is raising an exception and a dependency with yield \
             has a block with a bare except, or a block with except Exception, \
             and is not raising the exception again. Read more about it in the \
             docs: https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/#dependencies-with-yield-and-except",
        ),
    }
}

// ── Parameter extraction helpers ─────────────────────────────────────

fn extract_params_to_pydict_full<'py>(
    py: Python<'py>,
    params: &[ParamInfo],
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    query_multi: &HashMap<String, Vec<String>>,
    headers: &Option<HeaderMap>,
    // Content-Type captured cheaply before the body was consumed. Lets a
    // body-only route (no header/cookie/inject/file/form/dep params) skip the
    // full ``HeaderMap`` clone entirely while the body arm still sees the MIME.
    // ``None`` ⇒ fall back to reading ``headers`` (the non-body-fast-path case).
    content_type: Option<&str>,
    body_json: &Option<&serde_json::Value>,
    body_bytes: &[u8],
    multipart_fields: &mut Option<HashMap<String, Vec<ParsedField>>>,
    defers_extraction_errors: bool,
    lax_content_type: bool,
    // Precomputed at startup (RouteState) — skips the per-request ``pm_``
    // param-name scan below for routes without parameter-models.
    has_param_model: bool,
) -> Result<pyo3::Bound<'py, pyo3::types::PyDict>, Response> {
    let kwargs = pyo3::types::PyDict::new(py);
    // Accumulate per-field extraction errors so we can emit FA's
    // multi-error 422 shape (`?a=x&b=y&c=z` → three int_parsing
    // entries) in a single response. We only stop extracting early
    // when a body-level error fires (it short-circuits the whole
    // request).
    let mut extraction_errors: Vec<serde_json::Value> = Vec::new();
    // Stash the raw body for the DEFERRED-extraction path so Python
    // can populate ``RequestValidationError.body`` on a 422.
    // ``test_handling_errors/test_tutorial005`` asserts ``exc.body``
    // equals the original JSON body dict. Skipped when the handler is
    // a raw user function (no deferral wrapper) to avoid leaking the
    // sentinel kwarg.
    if defers_extraction_errors && !body_bytes.is_empty() {
        if let Ok(raw_str) = std::str::from_utf8(body_bytes) {
            let _ = kwargs.set_item("__fastapi_turbo_raw_body_str__", raw_str);
        }
    }

    for param in params {
        if !param.is_handler_param {
            continue;
        }

        match param.kind.as_str() {
            "path" => {
                let p_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
                if let Some(raw) = path_map.get(p_lookup) {
                    // Rust fast lane for unconstrained int/str path params —
                    // skips the Pydantic TypeAdapter round-trip. Any shape
                    // outside the strict lane (or no fast_path_coerce flag)
                    // falls through to the existing paths so lax coercions
                    // and 422 bodies stay FA-exact.
                    if param.fast_path_coerce {
                        if let Some(v) = fast_coerce_path_value(py, raw, &param.type_hint) {
                            let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
                            continue;
                        }
                    }
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, raw).into_any();
                        // Accumulate (don't short-circuit) so multiple bad PATH
                        // params surface ALL their errors in one 422, matching
                        // FA (`/p/{a}/{b}/{c}` with 3 bad ints → 3 int_parsing
                        // entries) — same contract as the query branch below.
                        match run_scalar_validator_detail(py, param, "path", &raw_py) {
                            Ok(validated) => {
                                let _ = kwargs.set_item(param.name_pystr(py), validated);
                            }
                            Err(mut errs) => {
                                extraction_errors.append(&mut errs);
                                continue;
                            }
                        }
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
                            }
                            None => {
                                extraction_errors.push(coercion_error_detail(
                                    "path",
                                    p_lookup,
                                    raw,
                                    &param.type_hint,
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
                    let values = query_multi.get(q_lookup).cloned().unwrap_or_default();
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
                                Some(coerced) => {
                                    let _ = list.append(coerced.bind(py));
                                }
                                None => {
                                    extraction_errors.push(coercion_error_detail_indexed(
                                        "query", q_lookup, idx, v, inner,
                                    ));
                                    any_err = true;
                                }
                            }
                        }
                        if !any_err {
                            if param.scalar_validator.is_some() {
                                // Feed the coerced list to the field TypeAdapter so
                                // container types get FA semantics: frozenset/set
                                // dedup, tuple arity (``tuple[int,int]`` rejects 3
                                // values). Plain list[...] validators are identity.
                                match run_scalar_validator_detail(py, param, "query", list.as_any())
                                {
                                    Ok(validated) => {
                                        let _ = kwargs.set_item(param.name_pystr(py), validated);
                                    }
                                    Err(mut errs) => extraction_errors.append(&mut errs),
                                }
                            } else {
                                let _ = kwargs.set_item(param.name_pystr(py), list);
                            }
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
                            Ok(validated) => {
                                let _ = kwargs.set_item(param.name_pystr(py), validated);
                            }
                            Err(mut errs) => {
                                extraction_errors.append(&mut errs);
                                continue;
                            }
                        }
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
                            }
                            None => {
                                extraction_errors.push(coercion_error_detail(
                                    "query",
                                    q_lookup,
                                    raw,
                                    &param.type_hint,
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
                    // Strict mode: Content-Type MUST be JSON.
                    // Lax mode: missing Content-Type is OK, but a
                    // declared non-JSON ``Content-Type`` still errors
                    // (FA parity — ``test_lax_post_with_text_plain_is_still_rejected``).
                    let ct_header = content_type.or_else(|| {
                        headers
                            .as_ref()
                            .and_then(|h| h.get("content-type"))
                            .and_then(|v| v.to_str().ok())
                    });
                    // Allocation-free case-insensitive check (was a per-request
                    // ``to_ascii_lowercase`` heap alloc): MIME head must be
                    // ``application/json`` or ``application/*+json``.
                    let ct_is_json = match ct_header {
                        Some(s) => {
                            let head = s.split(';').next().unwrap_or("").trim();
                            match head.get(..12) {
                                Some(p) if p.eq_ignore_ascii_case("application/") => {
                                    let rest = &head[12..];
                                    rest.eq_ignore_ascii_case("json")
                                        || rest
                                            .get(rest.len().saturating_sub(5)..)
                                            .is_some_and(|t| t.eq_ignore_ascii_case("+json"))
                                }
                                _ => false,
                            }
                        }
                        None => lax_content_type,
                    };
                    let body_validator = resolve_body_validator(py, param);
                    let val = if let Some(ref validator) = body_validator {
                        let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                        let result = if ct_is_json {
                            // Fast lane: the pre-bound native
                            // ``SchemaValidator.validate_json`` (fused jiter
                            // parse+validate — no Python frame). On ANY error
                            // re-run the FA wrapper so error shapes stay exact
                            // (json_invalid byte loc, model_attributes_type);
                            // the double parse only happens on the cold 422 path.
                            match param
                                .native_json_validator
                                .as_ref()
                                .map(|nv| nv.call1(py, (&py_bytes,)))
                            {
                                Some(Ok(v)) => Ok(v),
                                _ => validator.call_method1(py, "validate_json", (&py_bytes,)),
                            }
                        } else {
                            // Non-JSON Content-Type. For a raw-bytes body param
                            // pass the bytes OBJECT — lossy UTF-8 decoding would
                            // corrupt a binary payload (a 0xFF byte makes
                            // from_utf8 fail → unwrap_or("") → handler sees 0
                            // bytes). For other types pass the decoded string so
                            // Pydantic errors with model_attributes_type (FA parity).
                            let py_input = if param.type_hint == "bytes" {
                                pyo3::types::PyBytes::new(py, body_bytes).into_any()
                            } else {
                                let raw_str = std::str::from_utf8(body_bytes).unwrap_or("");
                                pyo3::types::PyString::new(py, raw_str).into_any()
                            };
                            validator.call_method1(py, "validate_python", (py_input,))
                        };
                        match result {
                            Ok(v) => v,
                            Err(e) => {
                                // FA parity: our FA-body-validator
                                // raises HTTPException for body-parse
                                // errors (400 "There was an error
                                // parsing the body"). Surface it as
                                // an HTTP error, not a 422.
                                if e.value(py).getattr("status_code").is_ok() {
                                    return Err(crate::responses::pyerr_to_response(py, &e));
                                }
                                if param.name == "_combined_body" {
                                    return Err(pydantic_error_response_combined_with_body(
                                        py, &e, "body", body_bytes,
                                    ));
                                }
                                return Err(pydantic_error_response_with_body(
                                    py, &e, "body", body_bytes,
                                ));
                            }
                        }
                    } else if let Some(json_val) = body_json {
                        // No Pydantic model — pass as dict
                        serde_to_pyobj(py, json_val)
                    } else {
                        // Raw bytes couldn't be parsed as JSON
                        let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                        py_bytes.into_any().unbind()
                    };
                    let _ = kwargs.set_item(param.name_pystr(py), val.bind(py));
                } else if apply_default(py, &kwargs, param) {
                    // Default applied (incl. a single ``Optional[Model]`` body whose
                    // default is ``None`` — absent body → None, not a built model).
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
                        let _ = kwargs.set_item(param.name_pystr(py), list);
                    } else if apply_default(py, &kwargs, param) {
                        // default
                    } else if param.required {
                        let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                        extraction_errors.push(missing_error_detail("header", loc_name));
                        continue;
                    }
                    continue;
                }
                let header_val = headers
                    .as_ref()
                    .and_then(|h| h.get(lookup.as_str()))
                    .and_then(|v| v.to_str().ok());
                if let Some(raw) = header_val {
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, raw).into_any();
                        match run_scalar_validator_detail(py, param, "header", &raw_py) {
                            Ok(validated) => {
                                let _ = kwargs.set_item(param.name_pystr(py), validated);
                            }
                            Err(mut errs) => {
                                extraction_errors.append(&mut errs);
                                continue;
                            }
                        }
                    } else {
                        match try_coerce_str_to_py(py, raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
                            }
                            None => {
                                // Use the alias (hyphenated wire name)
                                // rather than the underscored Python
                                // identifier so `loc` matches FastAPI.
                                let loc_name = param.alias.as_deref().unwrap_or(&param.name);
                                extraction_errors.push(coercion_error_detail(
                                    "header",
                                    loc_name,
                                    raw,
                                    &param.type_hint,
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
                let cookie_val = headers
                    .as_ref()
                    .and_then(|h| h.get("cookie"))
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| parse_cookie_value(s, lookup));
                if let Some(raw) = cookie_val {
                    if param.scalar_validator.is_some() {
                        let raw_py = pyo3::types::PyString::new(py, &raw).into_any();
                        let validated = run_scalar_validator(py, param, "cookie", &raw_py)?;
                        let _ = kwargs.set_item(param.name_pystr(py), validated);
                    } else {
                        match try_coerce_str_to_py(py, &raw, &param.type_hint) {
                            Some(v) => {
                                let _ = kwargs.set_item(param.name_pystr(py), v.bind(py));
                            }
                            None => {
                                return Err(coercion_error_response(
                                    "cookie",
                                    &param.name,
                                    &raw,
                                    &param.type_hint,
                                ))
                            }
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
                    return Err(validation_error_response(
                        "cookie",
                        loc_name,
                        "field required",
                    ));
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
                let wants_raw_bytes = param.type_hint == "bytes" || param.type_hint == "list_bytes";
                let wants_list = param.type_hint.starts_with("list_");
                let alias_name = param.alias.as_deref().unwrap_or(&param.name);
                let fields = multipart_fields.as_mut().and_then(|m| m.remove(alias_name));
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        if !wants_list && fs.len() == 1 {
                            if wants_raw_bytes {
                                let field = fs.remove(0);
                                let py_bytes = pyo3::types::PyBytes::new(py, &field.data);
                                let _ = kwargs.set_item(param.name_pystr(py), py_bytes);
                            } else {
                                let wrapped = make_upload_file(py, fs.remove(0)).map_err(|_e| {
                                    validation_error_response("body", alias_name, "alloc")
                                })?;
                                let _ = kwargs.set_item(param.name_pystr(py), wrapped);
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
                            let _ = kwargs.set_item(param.name_pystr(py), list);
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
                            let _ = kwargs.set_item(param.name_pystr(py), v);
                        } else if param.required {
                            // Collect all missing-field errors before
                            // surfacing — FA emits one 422 with every
                            // missing form/file field in the detail list.
                            // Accumulate unconditionally (like the "form"
                            // arm below); the post-loop block returns the
                            // combined 422 for non-deferring routes.
                            extraction_errors.push(missing_error_detail("body", alias_name));
                            continue;
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
                let fields = multipart_fields.as_mut().and_then(|m| m.remove(alias_name));
                let wants_list = param.type_hint.starts_with("list_");
                match fields {
                    Some(mut fs) if !fs.is_empty() => {
                        if wants_list {
                            // Collect every occurrence into a Python list
                            // so ``tags=a&tags=b`` hydrates a list field
                            // (or a BaseModel ``tags: list[str]`` when the
                            // form body is a parameter-model expansion).
                            //
                            // Coerce each element to the declared inner
                            // type — ``tuple[int, int]`` / ``list[float]``
                            // etc. must arrive at the handler as ints /
                            // floats, not raw strings. Matches the query
                            // extractor's ``list_<inner>`` behaviour.
                            let inner = &param.type_hint[5..]; // strip "list_"
                            let coerce_inner = !inner.is_empty() && inner != "str";
                            let list = pyo3::types::PyList::empty(py);
                            let mut any_err = false;
                            let mut has_file = false;
                            for (idx, f) in fs.drain(..).enumerate() {
                                if f.filename.is_some() {
                                    has_file = true;
                                    let wrapped = make_upload_file(py, f).map_err(|_e| {
                                        validation_error_response("body", alias_name, "alloc")
                                    })?;
                                    let _ = list.append(wrapped);
                                } else {
                                    let text = String::from_utf8_lossy(&f.data).into_owned();
                                    if coerce_inner {
                                        match try_coerce_str_to_py(py, &text, inner) {
                                            Some(v) => {
                                                let _ = list.append(v.bind(py));
                                            }
                                            None => {
                                                extraction_errors.push(
                                                    coercion_error_detail_indexed(
                                                        "body", alias_name, idx, &text, inner,
                                                    ),
                                                );
                                                any_err = true;
                                            }
                                        }
                                    } else {
                                        let _ = list.append(pyo3::types::PyString::new(py, &text));
                                    }
                                }
                            }
                            if !any_err {
                                if !has_file && param.scalar_validator.is_some() {
                                    // Run the field TypeAdapter on the coerced list
                                    // for container semantics (frozenset/set dedup,
                                    // ``tuple[int,int]`` arity). Skip when any item
                                    // is an UploadFile (the validator would reject
                                    // it). loc is "body" for form fields.
                                    match run_scalar_validator_detail(
                                        py,
                                        param,
                                        "body",
                                        list.as_any(),
                                    ) {
                                        Ok(validated) => {
                                            let _ =
                                                kwargs.set_item(param.name_pystr(py), validated);
                                        }
                                        Err(mut errs) => extraction_errors.append(&mut errs),
                                    }
                                } else {
                                    let _ = kwargs.set_item(param.name_pystr(py), list);
                                }
                            }
                        } else {
                            let field = fs.remove(0);
                            if field.filename.is_some() {
                                let wrapped = make_upload_file(py, field).map_err(|_e| {
                                    validation_error_response("body", alias_name, "alloc")
                                })?;
                                let _ = kwargs.set_item(param.name_pystr(py), wrapped);
                            } else {
                                let text = String::from_utf8_lossy(&field.data).into_owned();
                                // FA parity: an empty form field on an
                                // Optional/non-required param uses the
                                // default (usually None). Without this,
                                // ``age=Form(None)`` + ``age=`` fails to
                                // parse as int and returns 422.
                                if text.is_empty()
                                    && !param.required
                                    && apply_default(py, &kwargs, param)
                                {
                                    continue;
                                }
                                if param.scalar_validator.is_some() {
                                    let raw_py = pyo3::types::PyString::new(py, &text).into_any();
                                    let validated =
                                        run_scalar_validator(py, param, "body", &raw_py)?;
                                    let _ = kwargs.set_item(param.name_pystr(py), validated);
                                } else {
                                    match try_coerce_str_to_py(py, &text, &param.type_hint) {
                                        Some(v) => {
                                            let _ =
                                                kwargs.set_item(param.name_pystr(py), v.bind(py));
                                        }
                                        None => {
                                            return Err(coercion_error_response(
                                                "body",
                                                alias_name,
                                                &text,
                                                &param.type_hint,
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
                            let _ = kwargs.set_item(param.name_pystr(py), v);
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
        let _ = kwargs.set_item("__fastapi_turbo_extraction_errors__", err_json);
    }

    // Expose RAW request dicts so param-model builders can feed them
    // to ``model_validate`` — FA's error.input for a param-model
    // includes the WHOLE request dict, not just the fields the model
    // declares. Only populate when at least one synthetic
    // parameter-model extraction step is present (names start with
    // ``pm_``; precomputed at startup), so routes without param-models
    // don't spend cycles serializing raw dicts into kwargs.
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
            let _ = kwargs.set_item("__fastapi_turbo_raw_query__", qd);
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
                let _ = kwargs.set_item("__fastapi_turbo_raw_headers__", hd);
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
                let _ = kwargs.set_item("__fastapi_turbo_raw_cookies__", cd);
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
                let _ = kwargs.set_item("__fastapi_turbo_raw_form__", fd);
            }
        }
    }

    Ok(kwargs)
}

/// Extract a single param into the resolved HashMap (slow path for dep handlers).
#[allow(clippy::too_many_arguments)]
fn extract_single_param(
    py: Python<'_>,
    param: &ParamInfo,
    path_map: &HashMap<String, String>,
    query_params: &HashMap<String, String>,
    query_multi: &HashMap<String, Vec<String>>,
    headers: &Option<HeaderMap>,
    body_json: &Option<serde_json::Value>,
    body_bytes: &[u8],
    multipart_fields: &mut Option<HashMap<String, Vec<ParsedField>>>,
    resolved: &mut HashMap<String, Py<PyAny>>,
    accum: &mut Vec<serde_json::Value>,
) -> Result<(), Response> {
    // Scalar (path/query/header/cookie) missing/coercion errors are PUSHED
    // into ``accum`` (the caller emits one combined 422 across all extra
    // deps — FA accumulates every missing required dep-input, not just the
    // first). Body-level errors still short-circuit with ``Err(Response)``
    // (a combined-body 422 already lists every missing body field).
    match param.kind.as_str() {
        "path" => {
            let p_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
            // Look up by the alias-aware key: a path param shared with a dependency
            // is emitted with a synthetic name (``_dep0__user_id``) but the matchit
            // capture key is the real name (``user_id``) carried in ``alias``.
            if let Some(raw) = path_map.get(p_lookup) {
                resolved.insert(
                    param.name.clone(),
                    coerce_str_to_py(py, raw, &param.type_hint),
                );
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                accum.push(missing_error_detail("path", p_lookup));
            }
        }
        "query" => {
            let q_lookup: &str = param.alias.as_deref().unwrap_or(&param.name);
            // List types collect ALL values for repeated ``?k=a&k=b`` — a
            // param-model field typed ``list[str]`` must see both, not the
            // single last-wins value from ``query_params``.
            if param.type_hint.starts_with("list_") {
                let values = query_multi.get(q_lookup).cloned().unwrap_or_default();
                if values.is_empty() {
                    if param.has_default {
                        let v = match &param.default_value {
                            Some(d) => d.clone_ref(py),
                            None => py.None(),
                        };
                        resolved.insert(param.name.clone(), v);
                    } else if param.required {
                        accum.push(missing_error_detail("query", q_lookup));
                    }
                } else {
                    let inner = &param.type_hint[5..]; // strip "list_"
                    let list = pyo3::types::PyList::empty(py);
                    for v in &values {
                        let coerced = coerce_str_to_py(py, v, inner);
                        let _ = list.append(coerced.bind(py));
                    }
                    resolved.insert(param.name.clone(), list.into_any().unbind());
                }
            } else if let Some(raw) = query_params.get(q_lookup) {
                resolved.insert(
                    param.name.clone(),
                    coerce_str_to_py(py, raw, &param.type_hint),
                );
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                accum.push(missing_error_detail("query", q_lookup));
            }
        }
        "body" => {
            let is_combined = param.name == "_combined_body";
            let body_validator = resolve_body_validator(py, param);
            if !body_bytes.is_empty() {
                if let Some(ref validator) = body_validator {
                    // Validate raw bytes directly (FA shapes). On error,
                    // remap to FA's combined/with-body 422 (alias-aware loc,
                    // top-level ``input=None``) instead of a raw 500.
                    // A COMBINED body (embed/multiple) whose JSON is NOT an object
                    // (e.g. ``[]``): real FA extracts each field → all missing. Feed
                    // ``{}`` so the validator emits per-field missing (loc=["body",
                    // field], input=None) instead of a top-level model_attributes_type.
                    let combined_non_object = is_combined
                        && serde_json::from_slice::<serde_json::Value>(body_bytes)
                            .map(|v| !v.is_object())
                            .unwrap_or(false);
                    let py_bytes = if combined_non_object {
                        pyo3::types::PyBytes::new(py, b"{}")
                    } else {
                        pyo3::types::PyBytes::new(py, body_bytes)
                    };
                    let result = validator.call_method1(py, "validate_json", (py_bytes,));
                    match result {
                        Ok(v) => {
                            resolved.insert(param.name.clone(), v);
                        }
                        Err(e) => {
                            // FA body-parse errors (HTTPException) win as-is.
                            if e.value(py).getattr("status_code").is_ok() {
                                return Err(crate::responses::pyerr_to_response(py, &e));
                            }
                            if is_combined {
                                if combined_non_object {
                                    return Err(pydantic_error_response_combined(py, &e, "body"));
                                }
                                return Err(pydantic_error_response_combined_with_body(
                                    py, &e, "body", body_bytes,
                                ));
                            }
                            return Err(pydantic_error_response_with_body(
                                py, &e, "body", body_bytes,
                            ));
                        }
                    }
                } else if let Some(ref json_val) = body_json {
                    // No Pydantic model — pass the parsed dict through.
                    resolved.insert(param.name.clone(), serde_to_pyobj(py, json_val));
                } else {
                    // Raw bytes couldn't be parsed as JSON — hand them on.
                    let py_bytes = pyo3::types::PyBytes::new(py, body_bytes);
                    resolved.insert(param.name.clone(), py_bytes.into_any().unbind());
                }
            } else if is_combined && !param.required && param.model_class.is_some() {
                // Optional COMBINED body absent (e.g. ``Body(embed=True) = None``
                // where every embedded field is optional): build it from defaults
                // (validate ``{}``) so the getters see field defaults — FA returns
                // the defaulted model. A single ``Optional[Model]`` body is NOT
                // combined → falls to the default (None) below.
                let empty = pyo3::types::PyBytes::new(py, b"{}");
                let built = if let Some(ref validator) = param.cached_validator {
                    validator.call_method1(py, "validate_json", (empty,))
                } else {
                    param
                        .model_class
                        .as_ref()
                        .unwrap()
                        .getattr(py, "__pydantic_validator__")
                        .and_then(|v| v.call_method1(py, "validate_json", (empty,)))
                };
                match built {
                    Ok(v) => {
                        resolved.insert(param.name.clone(), v);
                    }
                    Err(_) => {
                        let v = match &param.default_value {
                            Some(d) => d.clone_ref(py),
                            None => py.None(),
                        };
                        resolved.insert(param.name.clone(), v);
                    }
                }
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                // Empty body + required combined body: feed ``{}`` so the
                // validator emits per-field missing errors (loc=["body",<field>]).
                if is_combined {
                    if let Some(ref validator) = param.cached_validator {
                        let empty = pyo3::types::PyBytes::new(py, b"{}");
                        if let Err(e) = validator.call_method1(py, "validate_json", (empty,)) {
                            if e.value(py).getattr("status_code").is_ok() {
                                return Err(crate::responses::pyerr_to_response(py, &e));
                            }
                            return Err(pydantic_error_response_combined(py, &e, "body"));
                        }
                    }
                    return Err(missing_body_error());
                }
                return Err(validation_error_response(
                    "body",
                    &param.name,
                    "field required",
                ));
            }
        }
        "header" => {
            let loc_name = param.alias.as_deref().unwrap_or(&param.name);
            let lookup = loc_name.to_lowercase();
            let header_val = headers
                .as_ref()
                .and_then(|h| h.get(lookup.as_str()))
                .and_then(|v| v.to_str().ok());
            if let Some(raw) = header_val {
                resolved.insert(
                    param.name.clone(),
                    coerce_str_to_py(py, raw, &param.type_hint),
                );
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                accum.push(missing_error_detail("header", loc_name));
            }
        }
        "cookie" => {
            let loc_name = param.alias.as_deref().unwrap_or(&param.name);
            let cookie_val = headers
                .as_ref()
                .and_then(|h| h.get("cookie"))
                .and_then(|v| v.to_str().ok())
                // Alias-aware: a Cookie() consumed as a dependency input has a
                // synthetic ``param.name`` (``_dep0__last_query``) but the real
                // cookie name is in ``alias`` (loc_name).
                .and_then(|s| parse_cookie_value(s, loc_name));
            if let Some(raw) = cookie_val {
                resolved.insert(
                    param.name.clone(),
                    coerce_str_to_py(py, &raw, &param.type_hint),
                );
            } else if param.has_default {
                let v = match &param.default_value {
                    Some(d) => d.clone_ref(py),
                    None => py.None(),
                };
                resolved.insert(param.name.clone(), v);
            } else if param.required {
                accum.push(missing_error_detail("cookie", loc_name));
            }
        }
        "form" | "file" => {
            // Dependency-input form/file fields (Form model-expansion). Values are
            // re-validated by the model builder, so plain strings / UploadFiles
            // suffice here; absent fields fall back to the default (e.g.
            // _PM_MISSING) so the model applies its own default/missing logic.
            let alias_name = param.alias.as_deref().unwrap_or(&param.name);
            let wants_list = param.type_hint.starts_with("list_");
            let wants_raw_bytes = param.type_hint == "bytes" || param.type_hint == "list_bytes";
            let fields = multipart_fields.as_mut().and_then(|m| m.remove(alias_name));
            match fields {
                Some(mut fs) if !fs.is_empty() => {
                    if wants_list {
                        let list = pyo3::types::PyList::empty(py);
                        for f in fs.drain(..) {
                            if f.filename.is_some() && !wants_raw_bytes {
                                if let Ok(uf) = make_upload_file(py, f) {
                                    let _ = list.append(uf);
                                }
                            } else if wants_raw_bytes {
                                let _ = list.append(pyo3::types::PyBytes::new(py, &f.data));
                            } else {
                                let text = String::from_utf8_lossy(&f.data).into_owned();
                                let _ = list.append(pyo3::types::PyString::new(py, &text));
                            }
                        }
                        resolved.insert(param.name.clone(), list.into_any().unbind());
                    } else {
                        let f = fs.remove(0);
                        let val = if f.filename.is_some() && !wants_raw_bytes {
                            make_upload_file(py, f)
                                .map(|uf| uf.unbind())
                                .unwrap_or_else(|_| py.None())
                        } else if wants_raw_bytes {
                            pyo3::types::PyBytes::new(py, &f.data).into_any().unbind()
                        } else {
                            let text = String::from_utf8_lossy(&f.data).into_owned();
                            coerce_str_to_py(py, &text, &param.type_hint)
                        };
                        resolved.insert(param.name.clone(), val);
                    }
                }
                _ => {
                    if param.has_default {
                        let v = match &param.default_value {
                            Some(d) => d.clone_ref(py),
                            None => py.None(),
                        };
                        resolved.insert(param.name.clone(), v);
                    } else if param.required {
                        return Err(validation_error_response(
                            "body",
                            alias_name,
                            "field required",
                        ));
                    }
                }
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
            let status = StatusCode::from_u16(status).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY);
            return Response::builder()
                .status(status)
                .header("content-type", ct)
                .body(axum::body::Body::from(body_bytes))
                .unwrap();
        }
    }
    // Default response (no user exception_handler) — strip any ``body``
    // that was plumbed for the handler's RVE.body, since FA's default
    // 422 shape is ``{"detail": [...]}`` with no body.
    let default_json = match detail_json {
        serde_json::Value::Object(mut m) => {
            m.remove("body");
            serde_json::Value::Object(m).to_string()
        }
        other => other.to_string(),
    };
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        [("content-type", "application/json")],
        default_json,
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
fn coercion_error_detail(loc: &str, name: &str, raw: &str, type_hint: &str) -> serde_json::Value {
    let (err_type, msg) = match type_hint {
        "int" => (
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        "float" => (
            "float_parsing",
            "Input should be a valid number, unable to parse string as a number",
        ),
        "bool" => (
            "bool_parsing",
            "Input should be a valid boolean, unable to interpret input",
        ),
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
        "int" => (
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        "float" => (
            "float_parsing",
            "Input should be a valid number, unable to parse string as a number",
        ),
        "bool" => (
            "bool_parsing",
            "Input should be a valid boolean, unable to interpret input",
        ),
        _ => ("value_error", "Value error"),
    };
    serde_json::json!({
        "type": err_type,
        "loc": [loc, name, index],
        "msg": msg,
        "input": raw,
    })
}

/// Build a 422 response for a str→type coercion failure (int_parsing, etc.),
/// in Pydantic-v2 format.
fn coercion_error_response(loc: &str, name: &str, raw: &str, type_hint: &str) -> Response {
    let (err_type, msg) = match type_hint {
        "int" => (
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        "float" => (
            "float_parsing",
            "Input should be a valid number, unable to parse string as a number",
        ),
        "bool" => (
            "bool_parsing",
            "Input should be a valid boolean, unable to interpret input",
        ),
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

fn pydantic_error_response_with_body(
    py: Python<'_>,
    err: &PyErr,
    loc_prefix: &str,
    body_bytes: &[u8],
) -> Response {
    pydantic_error_response_with_loc_body(py, err, &[loc_prefix], false, body_bytes)
}

fn pydantic_error_response_combined_with_body(
    py: Python<'_>,
    err: &PyErr,
    loc_prefix: &str,
    body_bytes: &[u8],
) -> Response {
    pydantic_error_response_with_loc_body(py, err, &[loc_prefix], true, body_bytes)
}

fn pydantic_error_response_with_loc_body(
    py: Python<'_>,
    err: &PyErr,
    loc_prefix: &[&str],
    strip_missing_input: bool,
    body_bytes: &[u8],
) -> Response {
    // Re-uses the existing detail builder, then injects ``body`` into
    // the outer JSON so ``_rust_validation_handler`` can populate
    // ``RequestValidationError.body`` (FA parity —
    // ``test_handling_errors/test_tutorial005`` asserts).
    let details = pydantic_error_details(py, err, loc_prefix, strip_missing_input);
    let primary_loc = loc_prefix.first().copied().unwrap_or("");
    if details.is_empty() {
        return validation_error_response(primary_loc, "", &format!("{err}"));
    }
    let mut wrapper = serde_json::Map::new();
    wrapper.insert("detail".into(), serde_json::Value::Array(details));
    if !body_bytes.is_empty() {
        if let Ok(s) = std::str::from_utf8(body_bytes) {
            let parsed: Option<serde_json::Value> = serde_json::from_str(s).ok();
            if let Some(v) = parsed {
                wrapper.insert("body".into(), v);
            } else {
                wrapper.insert("body".into(), serde_json::Value::String(s.to_string()));
            }
        }
    }
    dispatch_validation_error(serde_json::Value::Object(wrapper))
}

fn pydantic_error_response_combined(py: Python<'_>, err: &PyErr, loc_prefix: &str) -> Response {
    // strip_missing_input=true; the details-builder keeps the input
    // for NESTED missing errors (loc length > 2) and nulls it only
    // for top-level missing.
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
                if let Some(m) = d
                    .get_item("msg")
                    .ok()
                    .flatten()
                    .and_then(|v| v.extract::<String>().ok())
                {
                    // FastAPI post-processes a handful of Pydantic-v2
                    // wordings to match its historical error strings
                    // (array→list, object→dictionary, duration→timedelta,
                    // etc.). Apply the same substitutions here.
                    let m2 = fastapi_normalize_error_msg(&m);
                    obj.insert("msg".into(), serde_json::Value::String(m2));
                }
                // input field — best-effort serialize to JSON via python's json module.
                // FA parity: null ``input`` for TOP-LEVEL missing body
                // fields only (``loc.len() <= 2``). For nested missing
                // errors (``["body","item","price"]``), preserve the
                // partial input Pydantic provided.
                let is_missing_err = obj
                    .get("type")
                    .and_then(|v| v.as_str())
                    .map(|s| s == "missing")
                    .unwrap_or(false);
                let loc_len_2 = obj
                    .get("loc")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                if strip_missing_input && is_missing_err && loc_len_2 <= 2 {
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
    // No-op replacements kept as anchors — if Pydantic ever renames
    // ``"valid set"`` we want a single place to patch. Clippy's
    // ``replacing text with itself`` warning is silenced via the
    // crate-level allow in ``lib.rs``.
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
                let unquoted = if raw.len() >= 2 && raw.starts_with('"') && raw.ends_with('"') {
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
        "int" => raw
            .trim()
            .parse::<i64>()
            .ok()
            .map(|i| i.into_pyobject(py).expect("int").into_any().unbind()),
        "float" => raw
            .trim()
            .parse::<f64>()
            .ok()
            .map(|f| f.into_pyobject(py).expect("float").into_any().unbind()),
        "bool" => {
            // Pydantic-v2's bool coercion accepts `t/f/y/n` and capitalized
            // forms in addition to the usual true/false spellings. Match
            // that set so FastAPI and fastapi-turbo agree on `?flag=t`.
            let lower = raw.to_ascii_lowercase();
            match lower.as_str() {
                "true" | "t" | "1" | "yes" | "y" | "on" => Some(
                    pyo3::types::PyBool::new(py, true)
                        .to_owned()
                        .into_any()
                        .unbind(),
                ),
                "false" | "f" | "0" | "no" | "n" | "off" => Some(
                    pyo3::types::PyBool::new(py, false)
                        .to_owned()
                        .into_any()
                        .unbind(),
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
        assert_eq!(
            convert_path("/users/{user_id}/posts/{post_id}"),
            "/users/{user_id}/posts/{post_id}"
        );
    }

    #[test]
    fn test_convert_catch_all() {
        assert_eq!(
            convert_path("/files/{file_path:path}"),
            "/files/{*file_path}"
        );
    }

    #[test]
    fn test_convert_no_params() {
        assert_eq!(convert_path("/hello"), "/hello");
    }
}
