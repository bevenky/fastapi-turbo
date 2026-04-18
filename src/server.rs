use axum::routing::get;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tower_http::compression::CompressionLayer;
use tower_http::cors::{Any as CorsAny, AllowHeaders, AllowMethods, AllowOrigin, CorsLayer};
use tower_http::services::ServeDir;

use crate::router::{build_router, RouteInfo};

// ── Static file cache ──────────────────────────────────────────────────
//
// Cache small static files (<= 1 MB) in memory keyed by absolute path.
// We stat the file on each request to check mtime — if it changed, we
// re-read. Keeps the working set hot while allowing live reloads.

const STATIC_CACHE_MAX_BYTES: u64 = 1024 * 1024;

#[derive(Clone)]
struct CachedFile {
    bytes: bytes::Bytes,
    content_type: &'static str,
    mtime: SystemTime,
    /// Monotonic instant of the last mtime validation. We revalidate at most
    /// once per `STATIC_TTL`; within that window, serve from cache without
    /// hitting the filesystem.
    validated_at: std::time::Instant,
}

/// How often to re-check mtime for cached static files. A 1s window is
/// imperceptible for dev (edit → refresh still works) and eliminates the
/// `fs::metadata` syscall from the hot path in production.
const STATIC_TTL: std::time::Duration = std::time::Duration::from_secs(1);

static STATIC_CACHE: OnceLock<Mutex<HashMap<String, CachedFile>>> = OnceLock::new();

fn static_cache() -> &'static Mutex<HashMap<String, CachedFile>> {
    STATIC_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn mime_for(path: &str) -> &'static str {
    let p = path.to_ascii_lowercase();
    let ext = p.rsplit('.').next().unwrap_or("");
    match ext {
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "wasm" => "application/wasm",
        "txt" => "text/plain; charset=utf-8",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        _ => "application/octet-stream",
    }
}

/// Set of static-mount prefixes ("/static", etc.) — populated at server
/// start. `slashes_redirect_middleware` fast-paths these so we don't run
/// the declared-paths check on every static file request.
static STATIC_PREFIXES: OnceLock<Vec<String>> = OnceLock::new();

/// A tower Service that serves small files from memory with an mtime
/// check, falling back to `ServeDir` for large files, range requests,
/// and anything unusual. Uses `ServeDir` as its fallback inner service.
#[derive(Clone)]
struct CachedServeDir {
    root: std::path::PathBuf,
    inner: ServeDir,
}

impl CachedServeDir {
    fn new(_prefix: &str, root: std::path::PathBuf) -> Self {
        let inner = ServeDir::new(&root);
        Self { root, inner }
    }
}

impl<B> tower::Service<axum::http::Request<B>> for CachedServeDir
where
    B: axum::body::HttpBody + Send + 'static,
    B::Data: Send,
    B::Error: std::error::Error + Send + Sync + 'static,
    <ServeDir as tower::Service<axum::http::Request<B>>>::Future: Send + 'static,
    <ServeDir as tower::Service<axum::http::Request<B>>>::Response:
        axum::response::IntoResponse + Send + 'static,
    <ServeDir as tower::Service<axum::http::Request<B>>>::Error: Send + 'static,
{
    type Response = axum::response::Response;
    type Error = <ServeDir as tower::Service<axum::http::Request<B>>>::Error;
    type Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Self::Response, Self::Error>> + Send>,
    >;

    fn poll_ready(
        &mut self,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Result<(), Self::Error>> {
        <ServeDir as tower::Service<axum::http::Request<B>>>::poll_ready(&mut self.inner, cx)
    }

    fn call(&mut self, req: axum::http::Request<B>) -> Self::Future {
        let method = req.method().clone();
        let has_range = req.headers().contains_key(axum::http::header::RANGE);
        // NOTE: axum's `nest_service` strips the mount prefix before passing
        // the request to the inner service, so `req.uri().path()` here is
        // already relative (e.g., "/style.css" for "/static/style.css").
        let path = req.uri().path().to_string();

        // Only attempt cache for plain GET / HEAD without Range.
        if !has_range
            && (method == axum::http::Method::GET || method == axum::http::Method::HEAD)
        {
            let rel_clean = path.trim_start_matches('/');
            // Reject attempts to escape the root via ".."
            if rel_clean.split('/').any(|c| c == "..") {
                // Fall through to ServeDir which handles the 403 properly.
                let fut = self.inner.call(req);
                return Box::pin(async move {
                    let resp = fut.await?;
                    Ok(axum::response::IntoResponse::into_response(resp))
                });
            }
            let full_path = self.root.join(rel_clean);

            // Try the cache — fast path for small, unchanged files.
            if let Ok(full_str) = full_path.to_str().ok_or(()).map(str::to_owned) {
                let cache = static_cache();
                let cached = {
                    let g = cache.lock().unwrap();
                    g.get(&full_str).cloned()
                };

                if let Some(cf) = cached {
                    let now = std::time::Instant::now();
                    // TTL-skip path: if the mtime was validated recently,
                    // skip the fs::metadata syscall entirely. This is the
                    // hot loop for cached static serving.
                    if now.duration_since(cf.validated_at) < STATIC_TTL {
                        let body = if method == axum::http::Method::HEAD {
                            axum::body::Body::empty()
                        } else {
                            axum::body::Body::from(cf.bytes.clone())
                        };
                        return Box::pin(async move {
                            Ok(axum::response::Response::builder()
                                .status(axum::http::StatusCode::OK)
                                .header("content-type", cf.content_type)
                                .header("content-length", cf.bytes.len())
                                .body(body)
                                .unwrap())
                        });
                    }
                    // TTL expired — revalidate mtime
                    if let Ok(meta) = std::fs::metadata(&full_path) {
                        if let Ok(mt) = meta.modified() {
                            if mt == cf.mtime {
                                // Bump validated_at so we skip metadata checks again
                                let mut g = cache.lock().unwrap();
                                if let Some(entry) = g.get_mut(&full_str) {
                                    entry.validated_at = now;
                                }
                                drop(g);
                                let body = if method == axum::http::Method::HEAD {
                                    axum::body::Body::empty()
                                } else {
                                    axum::body::Body::from(cf.bytes.clone())
                                };
                                return Box::pin(async move {
                                    Ok(axum::response::Response::builder()
                                        .status(axum::http::StatusCode::OK)
                                        .header("content-type", cf.content_type)
                                        .header("content-length", cf.bytes.len())
                                        .body(body)
                                        .unwrap())
                                });
                            }
                        }
                    }
                }

                // Cache miss — if the file is small, read + insert now
                if let Ok(meta) = std::fs::metadata(&full_path) {
                    if meta.is_file() && meta.len() <= STATIC_CACHE_MAX_BYTES {
                        if let Ok(bytes) = std::fs::read(&full_path) {
                            let mtime = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
                            let ct = mime_for(&full_str);
                            let len = bytes.len();
                            let bytes = bytes::Bytes::from(bytes);
                            {
                                let mut g = cache.lock().unwrap();
                                g.insert(full_str, CachedFile {
                                    bytes: bytes.clone(),
                                    content_type: ct,
                                    mtime,
                                    validated_at: std::time::Instant::now(),
                                });
                            }
                            let body = if method == axum::http::Method::HEAD {
                                axum::body::Body::empty()
                            } else {
                                axum::body::Body::from(bytes)
                            };
                            return Box::pin(async move {
                                Ok(axum::response::Response::builder()
                                    .status(axum::http::StatusCode::OK)
                                    .header("content-type", ct)
                                    .header("content-length", len)
                                    .body(body)
                                    .unwrap())
                            });
                        }
                    }
                }
            }
        }

        // Fallback: delegate to ServeDir for range requests, large files,
        // directory listings, error cases.
        let fut = self.inner.call(req);
        Box::pin(async move {
            let resp = fut.await?;
            Ok(axum::response::IntoResponse::into_response(resp))
        })
    }
}

// ── OpenAPI / documentation HTML templates ─────────────────────────

const SWAGGER_UI_HTML: &str = r#"<!DOCTYPE html>
<html>
<head>
    <title>fastapi-rs - Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            url: '__OPENAPI_URL__',
            dom_id: '#swagger-ui',
            presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
            layout: 'StandaloneLayout',
        });
    </script>
</body>
</html>"#;

const REDOC_HTML: &str = r#"<!DOCTYPE html>
<html>
<head>
    <title>fastapi-rs - ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
    <redoc spec-url='__OPENAPI_URL__'></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body>
</html>"#;

/// Start the Axum HTTP server.  Called from Python.
///
/// This function blocks until the server shuts down (Ctrl-C).
#[pyfunction]
#[pyo3(signature = (routes, host, port, middlewares=vec![], openapi_json=None, docs_url=None, redoc_url=None, openapi_url=None, static_mounts=vec![], root_path=None, redirect_slashes=true, max_request_size=None, not_found_handler=None, app=None, validation_handler=None))]
pub fn run_server(
    py: Python<'_>,
    routes: Vec<RouteInfo>,
    host: String,
    port: u16,
    middlewares: Vec<Py<PyAny>>,
    openapi_json: Option<String>,
    docs_url: Option<String>,
    redoc_url: Option<String>,
    openapi_url: Option<String>,
    static_mounts: Vec<(String, String)>,
    root_path: Option<String>,
    redirect_slashes: bool,
    max_request_size: Option<usize>,
    not_found_handler: Option<Py<PyAny>>,
    app: Option<Py<PyAny>>,
    validation_handler: Option<Py<PyAny>>,
) -> PyResult<()> {
    // Stash the user's 404 handler so the Rust Router fallback can dispatch
    // through Python when nothing else matched. Set once per process.
    if let Some(h) = not_found_handler {
        let _ = crate::router::NOT_FOUND_HANDLER.set(h);
    }
    // Stash the FastAPI app instance so Request objects injected into handlers
    // expose request.app (vLLM / SGLang read request.app.state heavily).
    if let Some(a) = app {
        let _ = crate::router::APP_INSTANCE.set(a);
    }
    // Stash the validation-error dispatcher so body/query/path validation
    // failures route through `@exception_handler(RequestValidationError)`.
    if let Some(h) = validation_handler {
        let _ = crate::router::VALIDATION_HANDLER.set(h);
    }
    // Parse middleware config while we still have the GIL
    let mw_configs = parse_middleware_configs(py, &middlewares)?;

    // Release the GIL for the entire duration of the blocking server run.
    py.detach(|| {
        let rt = Runtime::new().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Failed to create tokio runtime: {e}"
            ))
        })?;

        // Record declared paths for the redirect_slashes middleware.
        {
            use std::collections::HashSet;
            let set: HashSet<String> = routes.iter().map(|r| r.path.clone()).collect();
            let _ = DECLARED_PATHS.set(set);
        }

        rt.block_on(async move {
            let (mut router, ws_router) = build_router(routes);

            // Pure Rust baseline endpoints — zero Python
            router = router.route("/_ping", get(|| async {
                (
                    axum::http::StatusCode::OK,
                    [("content-type", "application/json")],
                    r#"{"ping":"pong"}"#,
                )
            }));

            // Pure Rust WebSocket echo — measures Axum WS baseline (no Python)
            router = router.route("/_ws-echo", axum::routing::any(
                |ws: axum::extract::ws::WebSocketUpgrade| async {
                    ws.on_upgrade(crate::websocket::handle_ws_echo_rust)
                }
            ));

            // Add OpenAPI / documentation routes if enabled
            if let Some(ref schema_json) = openapi_json {
                let oa_url = openapi_url.as_deref().unwrap_or("/openapi.json");

                // Serve the OpenAPI JSON schema
                let json_clone = schema_json.clone();
                router = router.route(
                    oa_url,
                    get(move || async move {
                        (
                            [(axum::http::header::CONTENT_TYPE, "application/json")],
                            json_clone.clone(),
                        )
                    }),
                );

                // Swagger UI
                if let Some(ref docs_path) = docs_url {
                    let swagger_html = SWAGGER_UI_HTML.replace("__OPENAPI_URL__", oa_url);
                    router = router.route(
                        docs_path,
                        get(move || async move {
                            axum::response::Html(swagger_html.clone())
                        }),
                    );
                }

                // ReDoc
                if let Some(ref redoc_path) = redoc_url {
                    let redoc_html = REDOC_HTML.replace("__OPENAPI_URL__", oa_url);
                    router = router.route(
                        redoc_path,
                        get(move || async move {
                            axum::response::Html(redoc_html.clone())
                        }),
                    );
                }
            }

            // Register prefixes so the redirect_slashes middleware can
            // short-circuit for static file requests.
            let prefix_list: Vec<String> = static_mounts
                .iter()
                .map(|(p, _)| p.trim_end_matches('/').to_string())
                .collect();
            let _ = STATIC_PREFIXES.set(prefix_list.clone());

            // FastAPI/Starlette semantics: root_path is metadata only — it is
            // advertised in OpenAPI `servers` and used by `url_for`, but the
            // routing layer always matches paths as-written. The ASGI server or
            // reverse proxy is responsible for stripping the prefix before the
            // request reaches us. vLLM relies on this behaviour.
            let _ = &root_path;

            // Apply app-level middleware (CORS, GZip, etc.) to the MAIN
            // (HTTP) router. WebSocket routes are merged in AFTER middleware
            // so they bypass the CORS/compression stack — tower-http's
            // CorsLayer mutates the 101 Switching Protocols upgrade response
            // and breaks the WS handshake when applied to WS routes.
            let main_with_mw = apply_middlewares(router, &mw_configs);
            // Merge WS routes in at the top level (no CORS). Then attach the
            // FastAPI-style 404 fallback so it only fires when neither the
            // HTTP nor WS branches matched.
            let main_with_mw = crate::router::with_not_found_fallback(
                ws_router.merge(main_with_mw)
            );
            let mut app = axum::Router::new();
            for (prefix, directory) in &static_mounts {
                let svc = CachedServeDir::new(prefix, std::path::PathBuf::from(directory));
                app = app.nest_service(prefix, svc);
            }
            let mut app = app.fallback_service(main_with_mw);

            // redirect_slashes: trailing-slash redirect middleware.
            // Matches Starlette's `redirect_slashes=True` default.
            if redirect_slashes {
                app = app.layer(axum::middleware::from_fn(slashes_redirect_middleware));
            }

            // Non-preflight OPTIONS: FastAPI/Starlette's CORS intercepts
            // only actual cross-origin preflights (request has both
            // `Origin` AND `Access-Control-Request-Method`). tower-http's
            // CorsLayer is more lenient and returns 200 for any OPTIONS.
            // We add a pre-middleware that lets OPTIONS *without* those
            // headers fall through to method routing (→ 405 as expected).
            app = app.layer(axum::middleware::from_fn(non_preflight_options_middleware));

            // max_request_size: 413 Payload Too Large on oversized bodies.
            if let Some(limit) = max_request_size {
                app = app.layer(tower_http::limit::RequestBodyLimitLayer::new(limit));
            }

            let addr = format!("{host}:{port}");
            let listener = TcpListener::bind(&addr).await.map_err(|e| {
                pyo3::exceptions::PyOSError::new_err(format!(
                    "Failed to bind to {addr}: {e}"
                ))
            })?;

            println!("fastapi-rs running on http://{addr}");

            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
                .with_graceful_shutdown(shutdown_signal())
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Server error: {e}"
                    ))
                })?;

            Ok(())
        })
    })
}

/// Set of *declared* paths (verbatim) from the user's routes. Used by the
/// redirect_slashes middleware to decide whether the alternate form of a
/// requested URL is an actual registered route before redirecting.
static DECLARED_PATHS: std::sync::OnceLock<std::collections::HashSet<String>> =
    std::sync::OnceLock::new();

/// Pass-through middleware for non-preflight OPTIONS. Tower-http's
/// ``CorsLayer`` intercepts *every* OPTIONS request with configured
/// ``allow_methods`` and returns 200 — FastAPI/Starlette only do that
/// for true preflights (request carrying both ``Origin`` and
/// ``Access-Control-Request-Method`` headers). Here we detect the
/// non-preflight case and strip the method to something the CORS layer
/// ignores, effectively routing through to the normal method handler.
async fn non_preflight_options_middleware(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> axum::response::Response {
    if req.method() == axum::http::Method::OPTIONS {
        let has_origin = req.headers().contains_key("origin");
        let has_acrm = req.headers().contains_key("access-control-request-method");
        if !(has_origin && has_acrm) {
            // Not a preflight — bypass CORS by bolting a short-circuit
            // response that matches FastAPI's 405 behavior for OPTIONS on
            // non-OPTIONS routes. The inner router would produce 405 too,
            // but since CorsLayer sits between us and the router and will
            // override with 200, we just emit the 405 directly.
            return axum::response::Response::builder()
                .status(axum::http::StatusCode::METHOD_NOT_ALLOWED)
                .header("content-type", "application/json")
                .header("allow", "GET, POST, PUT, DELETE, PATCH, HEAD")
                .body(axum::body::Body::from(r#"{"detail":"Method Not Allowed"}"#))
                .unwrap();
        }
    }
    next.run(req).await
}

/// Middleware that 307-redirects between `/foo` ↔ `/foo/` ONLY when the
/// alternate form matches a declared route. Matches Starlette's
/// `redirect_slashes=True` behaviour. Implemented as a pre-filter: we check
/// the declared-paths set before handing off to the inner router.
async fn slashes_redirect_middleware(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> axum::response::Response {
    // Work with the URI path as a &str — avoid allocating a String on the
    // common hot path where we immediately fall through to next.run.
    let path: &str = req.uri().path();

    // Skip root / empty — most common static-serve case also hits here.
    if path.len() <= 1 {
        return next.run(req).await;
    }
    // Static mount prefixes never redirect.
    if let Some(prefixes) = STATIC_PREFIXES.get() {
        for p in prefixes {
            if path.starts_with(p.as_str()) {
                return next.run(req).await;
            }
        }
    }
    let declared = match DECLARED_PATHS.get() {
        Some(s) => s,
        None => return next.run(req).await,
    };
    // HashSet<String>::contains(&str) via Borrow<str> — zero-alloc lookup.
    if declared.contains(path) {
        return next.run(req).await;
    }

    // Only at this point do we potentially allocate (to build the alt form).
    let alternate: String = if path.ends_with('/') {
        path[..path.len() - 1].to_string()
    } else {
        format!("{path}/")
    };

    if !declared.contains(&alternate) {
        return next.run(req).await;
    }

    // Only redirect safe methods — POST/PUT/DELETE with body should not be
    // silently redirected (client must re-issue to canonical URL).
    if !matches!(
        req.method(),
        &axum::http::Method::GET | &axum::http::Method::HEAD
    ) {
        return next.run(req).await;
    }

    // Preserve query string in the redirect target
    let mut redirect_to = alternate;
    if let Some(q) = req.uri().query() {
        redirect_to.push('?');
        redirect_to.push_str(q);
    }

    axum::response::Response::builder()
        .status(axum::http::StatusCode::TEMPORARY_REDIRECT)
        .header("location", redirect_to)
        .body(axum::body::Body::empty())
        .unwrap()
}

/// Wait for Ctrl-C (SIGINT).
async fn shutdown_signal() {
    tokio::signal::ctrl_c()
        .await
        .expect("failed to install Ctrl-C handler");
    println!("\nfastapi-rs shutting down...");
}

// ── Middleware configuration ─────────────────────────────────────────

/// Parsed middleware configuration (GIL-free).
enum MiddlewareConfig {
    Cors {
        allow_origins: Vec<String>,
        allow_methods: Vec<String>,
        allow_headers: Vec<String>,
        allow_credentials: bool,
        max_age: u64,
        expose_headers: Vec<String>,
    },
    Gzip,
    TrustedHost {
        allowed_hosts: Vec<String>,
    },
    HttpsRedirect,
}

/// Parse Python middleware dicts into Rust structs while the GIL is held.
fn parse_middleware_configs(
    py: Python<'_>,
    middlewares: &[Py<PyAny>],
) -> PyResult<Vec<MiddlewareConfig>> {
    let mut configs = Vec::new();

    for mw_obj in middlewares {
        let mw = mw_obj.bind(py);
        let dict = mw.cast::<PyDict>()?;

        let mw_type: String = dict
            .get_item("type")?
            .ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(
                    "Middleware config must have a 'type' key",
                )
            })?
            .extract()?;

        match mw_type.as_str() {
            "cors" => {
                let allow_origins: Vec<String> = dict
                    .get_item("allow_origins")?
                    .map(|v| v.extract().unwrap_or_default())
                    .unwrap_or_default();

                let allow_methods: Vec<String> = dict
                    .get_item("allow_methods")?
                    .map(|v| v.extract().unwrap_or_default())
                    .unwrap_or_default();

                let allow_headers: Vec<String> = dict
                    .get_item("allow_headers")?
                    .map(|v| v.extract().unwrap_or_default())
                    .unwrap_or_default();

                let allow_credentials: bool = dict
                    .get_item("allow_credentials")?
                    .map(|v| v.extract().unwrap_or(false))
                    .unwrap_or(false);

                let max_age: u64 = dict
                    .get_item("max_age")?
                    .map(|v| v.extract().unwrap_or(600))
                    .unwrap_or(600);

                let expose_headers: Vec<String> = dict
                    .get_item("expose_headers")?
                    .map(|v| v.extract().unwrap_or_default())
                    .unwrap_or_default();

                configs.push(MiddlewareConfig::Cors {
                    allow_origins,
                    allow_methods,
                    allow_headers,
                    allow_credentials,
                    max_age,
                    expose_headers,
                });
            }
            "gzip" | "compress" => {
                configs.push(MiddlewareConfig::Gzip);
            }
            "trustedhost" => {
                let allowed_hosts: Vec<String> = dict
                    .get_item("allowed_hosts")?
                    .map(|v| v.extract().unwrap_or_default())
                    .unwrap_or_else(|| vec!["*".to_string()]);
                configs.push(MiddlewareConfig::TrustedHost { allowed_hosts });
            }
            "httpsredirect" => {
                configs.push(MiddlewareConfig::HttpsRedirect);
            }
            other => {
                eprintln!("fastapi-rs: unknown middleware type '{other}', skipping");
            }
        }
    }

    Ok(configs)
}

/// Apply Tower middleware layers to the router.
///
/// Layers are applied in reverse order so the first middleware in the user's
/// list wraps outermost (runs first on request, last on response), matching
/// the Starlette/FastAPI convention.
fn apply_middlewares(
    router: axum::Router,
    configs: &[MiddlewareConfig],
) -> axum::Router {
    let mut app = router;

    for config in configs.iter().rev() {
        match config {
            MiddlewareConfig::Cors {
                allow_origins,
                allow_methods,
                allow_headers,
                allow_credentials,
                max_age,
                expose_headers,
            } => {
                let cors = build_cors_layer(
                    allow_origins,
                    allow_methods,
                    allow_headers,
                    *allow_credentials,
                    *max_age,
                    expose_headers,
                );
                app = app.layer(cors);
            }
            MiddlewareConfig::Gzip => {
                app = app.layer(CompressionLayer::new());
            }
            MiddlewareConfig::TrustedHost { allowed_hosts } => {
                let hosts = allowed_hosts.clone();
                app = app.layer(axum::middleware::from_fn(move |req: axum::http::Request<axum::body::Body>, next: axum::middleware::Next| {
                    let hosts = hosts.clone();
                    async move {
                        // If wildcard, allow all
                        if hosts.iter().any(|h| h == "*") {
                            return next.run(req).await;
                        }

                        let host_header = req
                            .headers()
                            .get(axum::http::header::HOST)
                            .and_then(|v| v.to_str().ok())
                            .unwrap_or("");

                        // Strip port from host
                        let host_name = if host_header.contains(':') {
                            host_header.split(':').next().unwrap_or("")
                        } else {
                            host_header
                        }.to_lowercase();

                        let is_valid = hosts.iter().any(|pattern| {
                            let pattern = pattern.to_lowercase();
                            if pattern == host_name {
                                return true;
                            }
                            // Wildcard subdomain matching: "*.example.com"
                            if let Some(suffix) = pattern.strip_prefix('*') {
                                return host_name.ends_with(&suffix);
                            }
                            false
                        });

                        if is_valid {
                            next.run(req).await
                        } else {
                            axum::http::Response::builder()
                                .status(axum::http::StatusCode::BAD_REQUEST)
                                .body(axum::body::Body::from("Invalid host header"))
                                .unwrap()
                        }
                    }
                }));
            }
            MiddlewareConfig::HttpsRedirect => {
                app = app.layer(axum::middleware::from_fn(|req: axum::http::Request<axum::body::Body>, next: axum::middleware::Next| {
                    async move {
                        // Check X-Forwarded-Proto header or URI scheme
                        let is_https = req
                            .headers()
                            .get("x-forwarded-proto")
                            .and_then(|v| v.to_str().ok())
                            .map(|s| s.eq_ignore_ascii_case("https"))
                            .unwrap_or_else(|| {
                                req.uri().scheme_str().map(|s| s.eq_ignore_ascii_case("https")).unwrap_or(false)
                            });

                        if is_https {
                            return next.run(req).await;
                        }

                        // Build the redirect URL
                        let host = req
                            .headers()
                            .get(axum::http::header::HOST)
                            .and_then(|v| v.to_str().ok())
                            .unwrap_or("localhost");
                        let path_and_query = req.uri().path_and_query()
                            .map(|pq| pq.as_str())
                            .unwrap_or("/");
                        let redirect_url = format!("https://{host}{path_and_query}");

                        axum::http::Response::builder()
                            .status(axum::http::StatusCode::TEMPORARY_REDIRECT)
                            .header(axum::http::header::LOCATION, redirect_url)
                            .body(axum::body::Body::empty())
                            .unwrap()
                    }
                }));
            }
        }
    }

    app
}

/// Build a CorsLayer from the parsed configuration.
fn build_cors_layer(
    allow_origins: &[String],
    allow_methods: &[String],
    allow_headers: &[String],
    allow_credentials: bool,
    max_age: u64,
    expose_headers: &[String],
) -> CorsLayer {
    use http::header::HeaderName;
    use http::Method;
    use std::str::FromStr;

    let mut cors = CorsLayer::new();

    // When allow_credentials=true is combined with a wildcard spec for
    // origins, methods or headers, tower-http refuses and panics. Starlette's
    // CORSMiddleware quietly handles this by *mirroring* the request's value.
    // Match Starlette semantics so users who write the common
    //   CORSMiddleware(allow_origins=["*"], allow_credentials=True, ...)
    // (which vLLM and SGLang both do) don't get a surprise panic.
    let mirror_for_credentials = allow_credentials;

    // Origins
    if allow_origins.iter().any(|o| o == "*") {
        if mirror_for_credentials {
            cors = cors.allow_origin(AllowOrigin::mirror_request());
        } else {
            cors = cors.allow_origin(CorsAny);
        }
    } else if !allow_origins.is_empty() {
        let origins: Vec<http::HeaderValue> = allow_origins
            .iter()
            .filter_map(|o| o.parse().ok())
            .collect();
        cors = cors.allow_origin(origins);
    }

    // Methods
    if allow_methods.iter().any(|m| m == "*") {
        if mirror_for_credentials {
            cors = cors.allow_methods(AllowMethods::mirror_request());
        } else {
            cors = cors.allow_methods(CorsAny);
        }
    } else if !allow_methods.is_empty() {
        let methods: Vec<Method> = allow_methods
            .iter()
            .filter_map(|m| Method::from_str(m).ok())
            .collect();
        cors = cors.allow_methods(methods);
    }

    // Headers
    if allow_headers.iter().any(|h| h == "*") {
        if mirror_for_credentials {
            cors = cors.allow_headers(AllowHeaders::mirror_request());
        } else {
            cors = cors.allow_headers(CorsAny);
        }
    } else if !allow_headers.is_empty() {
        let headers: Vec<HeaderName> = allow_headers
            .iter()
            .filter_map(|h| HeaderName::from_str(h).ok())
            .collect();
        cors = cors.allow_headers(headers);
    }

    // Credentials
    if allow_credentials {
        cors = cors.allow_credentials(true);
    }

    // Max age
    cors = cors.max_age(std::time::Duration::from_secs(max_age));

    // Expose headers
    if !expose_headers.is_empty() {
        let headers: Vec<HeaderName> = expose_headers
            .iter()
            .filter_map(|h| HeaderName::from_str(h).ok())
            .collect();
        cors = cors.expose_headers(headers);
    }

    cors
}
