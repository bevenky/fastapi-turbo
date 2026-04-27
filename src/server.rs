use axum::routing::get;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tower_http::compression::CompressionLayer;
use tower_http::cors::{AllowHeaders, AllowMethods, AllowOrigin, Any as CorsAny, CorsLayer};
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
        if !has_range && (method == axum::http::Method::GET || method == axum::http::Method::HEAD) {
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
                                g.insert(
                                    full_str,
                                    CachedFile {
                                        bytes: bytes.clone(),
                                        content_type: ct,
                                        mtime,
                                        validated_at: std::time::Instant::now(),
                                    },
                                );
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

const SWAGGER_UI_HTML: &str = r######"
    <!DOCTYPE html>
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    <link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png">
    <title>fastapi-turbo - Swagger UI</title>
    </head>
    <body>
    <div id="swagger-ui">
    </div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <!-- `SwaggerUIBundle` is now available on the page -->
    <script>
    const ui = SwaggerUIBundle({
        url: '__OPENAPI_URL__',
    "dom_id": "#swagger-ui",
    "layout": "BaseLayout",
    "deepLinking": true,
    "showExtensions": true,
    "showCommonExtensions": true,
    oauth2RedirectUrl: window.location.origin + '__OAUTH2_REDIRECT_URL__',
    presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
        ],
    })
    </script>
    </body>
    </html>
    "######;

const SWAGGER_OAUTH2_REDIRECT_HTML: &str = r######"
    <!doctype html>
    <html lang="en-US">
    <head>
        <title>Swagger UI: OAuth2 Redirect</title>
    </head>
    <body>
    <script>
        'use strict';
        function run () {
            var oauth2 = window.opener.swaggerUIRedirectOauth2;
            var sentState = oauth2.state;
            var redirectUrl = oauth2.redirectUrl;
            var isValid, qp, arr;

            if (/code|token|error/.test(window.location.hash)) {
                qp = window.location.hash.substring(1).replace('?', '&');
            } else {
                qp = location.search.substring(1);
            }

            arr = qp.split("&");
            arr.forEach(function (v,i,_arr) { _arr[i] = '"' + v.replace('=', '":"') + '"';});
            qp = qp ? JSON.parse('{' + arr.join() + '}',
                    function (key, value) {
                        return key === "" ? value : decodeURIComponent(value);
                    }
            ) : {};

            isValid = qp.state === sentState;

            if ((
              oauth2.auth.schema.get("flow") === "accessCode" ||
              oauth2.auth.schema.get("flow") === "authorizationCode" ||
              oauth2.auth.schema.get("flow") === "authorization_code"
            ) && !oauth2.auth.code) {
                if (!isValid) {
                    oauth2.errCb({
                        authId: oauth2.auth.name,
                        source: "auth",
                        level: "warning",
                        message: "Authorization may be unsafe, passed state was changed in server. The passed state wasn't returned from auth server."
                    });
                }

                if (qp.code) {
                    delete oauth2.state;
                    oauth2.auth.code = qp.code;
                    oauth2.callback({auth: oauth2.auth, redirectUrl: redirectUrl});
                } else {
                    let oauthErrorMsg;
                    if (qp.error) {
                        oauthErrorMsg = "["+qp.error+"]: " +
                            (qp.error_description ? qp.error_description+ ". " : "no accessCode received from the server. ") +
                            (qp.error_uri ? "More info: "+qp.error_uri : "");
                    }

                    oauth2.errCb({
                        authId: oauth2.auth.name,
                        source: "auth",
                        level: "error",
                        message: oauthErrorMsg || "[Authorization failed]: no accessCode received from the server."
                    });
                }
            } else {
                oauth2.callback({auth: oauth2.auth, token: qp, isValid: isValid, redirectUrl: redirectUrl});
            }
            window.close();
        }

        if (document.readyState !== 'loading') {
            run();
        } else {
            document.addEventListener('DOMContentLoaded', function () {
                run();
            });
        }
    </script>
    </body>
    </html>
        "######;

const REDOC_HTML: &str = r#"
    <!DOCTYPE html>
    <html>
    <head>
    <title>fastapi-turbo - ReDoc</title>
    <!-- needed for adaptive design -->
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">

    <link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png">
    <!--
    ReDoc doesn't change outer page styles
    -->
    <style>
      body {
        margin: 0;
        padding: 0;
      }
    </style>
    </head>
    <body>
    <noscript>
        ReDoc requires Javascript to function. Please enable it to browse the documentation.
    </noscript>
    <redoc spec-url="__OPENAPI_URL__"></redoc>
    <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"> </script>
    </body>
    </html>
    "#;

/// Start the Axum HTTP server.  Called from Python.
///
/// This function blocks until the server shuts down (Ctrl-C).
#[pyfunction]
#[pyo3(signature = (routes, host, port, middlewares=vec![], openapi_json=None, docs_url=None, redoc_url=None, openapi_url=None, static_mounts=vec![], root_path=None, redirect_slashes=true, max_request_size=None, not_found_handler=None, app=None, validation_handler=None, swagger_ui_oauth2_redirect_url=None, swagger_ui_html=None, redoc_html=None))]
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
    swagger_ui_oauth2_redirect_url: Option<String>,
    swagger_ui_html: Option<String>,
    redoc_html: Option<String>,
) -> PyResult<()> {
    // Stash the user's 404 handler so the Rust Router fallback can dispatch
    // through Python when nothing else matched. Set once per process.
    // Always overwrite (RwLock, not OnceLock) so successive app.run()
    // calls — dozens of ephemeral apps in a test suite — each rebind
    // their own handlers rather than silently inheriting the first
    // one's. Passing ``None`` clears the slot.
    if let Ok(mut slot) = crate::router::NOT_FOUND_HANDLER.write() {
        *slot = not_found_handler;
    }
    if let Ok(mut slot) = crate::router::APP_INSTANCE.write() {
        *slot = app;
    }
    if let Ok(mut slot) = crate::router::VALIDATION_HANDLER.write() {
        *slot = validation_handler;
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

        // Build per-server declared-paths Arcs for the redirect_slashes
        // and non_preflight_options middlewares. Each server owns its
        // own view — sharing a global static across concurrent test apps
        // caused later-starting servers to overwrite an earlier server's
        // set (→ 404s on the earlier app's routes).
        let declared_paths_arc: std::sync::Arc<std::collections::HashSet<String>> = {
            let set: std::collections::HashSet<String> =
                routes.iter().map(|r| r.path.clone()).collect();
            std::sync::Arc::new(set)
        };
        let declared_options_paths_arc: std::sync::Arc<std::collections::HashSet<String>> = {
            let opts: std::collections::HashSet<String> = routes
                .iter()
                .filter(|r| r.methods.iter().any(|m| m.eq_ignore_ascii_case("OPTIONS")))
                .map(|r| r.path.clone())
                .collect();
            std::sync::Arc::new(opts)
        };
        // Per-path declared-method map AND a registration-ordered
        // ``Vec<(template, methods)>`` for the OPTIONS middleware.
        // First-match-wins parity with upstream FastAPI requires
        // walking templates in REGISTRATION order; the HashMap's
        // iteration order is non-deterministic. Earlier code used
        // "most specific" tiebreak (fewest ``{}`` segments) which
        // matched matchit's behaviour but diverged from Starlette
        // for overlapping literal/param routes (R27).
        let allow_methods_by_path_arc: std::sync::Arc<
            std::collections::HashMap<String, Vec<String>>,
        > = {
            let mut map: std::collections::HashMap<String, Vec<String>> =
                std::collections::HashMap::new();
            for r in &routes {
                let entry = map.entry(r.path.clone()).or_default();
                for m in &r.methods {
                    let up = m.to_ascii_uppercase();
                    if !entry.iter().any(|x| x == &up) {
                        entry.push(up);
                    }
                }
            }
            std::sync::Arc::new(map)
        };
        let allow_methods_in_order_arc: std::sync::Arc<Vec<(String, Vec<String>)>> = {
            let mut order: Vec<(String, Vec<String>)> = Vec::with_capacity(routes.len());
            for r in &routes {
                let methods: Vec<String> = r
                    .methods
                    .iter()
                    .map(|m| m.to_ascii_uppercase())
                    .collect();
                if let Some(existing) =
                    order.iter_mut().find(|(p, _)| p == &r.path)
                {
                    for m in methods {
                        if !existing.1.iter().any(|x| x == &m) {
                            existing.1.push(m);
                        }
                    }
                } else {
                    order.push((r.path.clone(), methods));
                }
            }
            std::sync::Arc::new(order)
        };
        // Also populate the legacy globals for any single-app code paths
        // (best-effort; authoritative is the per-server Arc).
        if let Ok(mut slot) = DECLARED_PATHS.write() {
            *slot = Some((*declared_paths_arc).clone());
        }
        if let Ok(mut slot) = DECLARED_OPTIONS_PATHS.write() {
            *slot = Some((*declared_options_paths_arc).clone());
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

            // Add OpenAPI / documentation routes if enabled. An empty
            // ``openapi_url`` means "disable OpenAPI + docs entirely" —
            // FA behavior tested by ``test_conditional_openapi``.
            // Docs UI is set up as long as ``openapi_url`` is set;
            // JSON endpoint is auto-registered only when
            // ``openapi_json`` is also provided (else Python handles it).
            if openapi_url.is_some() {
                let oa_url = openapi_url.as_deref().unwrap_or("/openapi.json");
                if oa_url.is_empty() {
                    // Skip openapi + docs routes; ``/openapi.json`` / ``/docs``
                    // simply return 404.
                } else {

                if let Some(ref schema_json) = openapi_json {
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
                }

                // Swagger UI — prefer Python-rendered HTML
                // (``get_swagger_ui_html``) when supplied by the
                // application; fall back to the embedded default
                // template. Python rendering honours FA kwargs like
                // ``swagger_ui_parameters`` and ``swagger_ui_init_oauth``.
                if let Some(ref docs_path) = docs_url {
                    let swagger_final = if let Some(s) = swagger_ui_html.clone() {
                        s
                    } else if let Some(ref oauth_redirect) = swagger_ui_oauth2_redirect_url {
                        SWAGGER_UI_HTML
                            .replace("__OPENAPI_URL__", oa_url)
                            .replace("__OAUTH2_REDIRECT_URL__", oauth_redirect)
                    } else {
                        SWAGGER_UI_HTML
                            .replace("__OPENAPI_URL__", oa_url)
                            .lines()
                            .filter(|l| !l.contains("oauth2RedirectUrl"))
                            .collect::<Vec<_>>()
                            .join("\n")
                    };
                    router = router.route(
                        docs_path,
                        get(move || async move {
                            axum::response::Html(swagger_final.clone())
                        }),
                    );
                    if let Some(ref oauth_redirect) = swagger_ui_oauth2_redirect_url {
                        router = router.route(
                            oauth_redirect,
                            get(|| async {
                                axum::response::Html(SWAGGER_OAUTH2_REDIRECT_HTML)
                            }),
                        );
                    }
                }

                // ReDoc — similarly prefer Python-rendered HTML.
                if let Some(ref redoc_path) = redoc_url {
                    let redoc_final = redoc_html.clone().unwrap_or_else(|| {
                        REDOC_HTML.replace("__OPENAPI_URL__", oa_url)
                    });
                    router = router.route(
                        redoc_path,
                        get(move || async move {
                            axum::response::Html(redoc_final.clone())
                        }),
                    );
                }
                } // end: oa_url non-empty
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
                let paths_arc = declared_paths_arc.clone();
                app = app.layer(axum::middleware::from_fn(
                    move |req: axum::http::Request<axum::body::Body>, next: axum::middleware::Next| {
                        let paths = paths_arc.clone();
                        async move { slashes_redirect_middleware_with_paths(req, next, paths).await }
                    },
                ));
            }

            // Non-preflight OPTIONS: FastAPI/Starlette's CORS intercepts
            // only actual cross-origin preflights (request has both
            // `Origin` AND `Access-Control-Request-Method`). tower-http's
            // CorsLayer is more lenient and returns 200 for any OPTIONS.
            // We add a pre-middleware that lets OPTIONS *without* those
            // headers fall through to method routing (→ 405 as expected).
            let opts_paths_arc = declared_options_paths_arc.clone();
            let allow_by_path_arc = allow_methods_by_path_arc.clone();
            let allow_in_order_arc = allow_methods_in_order_arc.clone();
            app = app.layer(axum::middleware::from_fn(
                move |req: axum::http::Request<axum::body::Body>, next: axum::middleware::Next| {
                    let paths = opts_paths_arc.clone();
                    let allow = allow_by_path_arc.clone();
                    let order = allow_in_order_arc.clone();
                    async move {
                        non_preflight_options_middleware_with_paths(
                            req, next, paths, allow, order,
                        ).await
                    }
                },
            ));

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

            // Publish the bound address so request scopes can populate
            // `scope["server"] = (host, port)` / `.scheme` — FastAPI fills
            // these from the ASGI scope dict, and user code reads them via
            // `request.url.hostname` / `.port`.
            let _ = crate::router::set_server_addr(host.clone(), port);

            println!("fastapi-turbo running on http://{addr}");

            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
                .with_graceful_shutdown(shutdown_signal_for_port(port))
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
/// Mutable so consecutive ``run_server()`` calls (test suites with
/// many ephemeral apps) replace rather than ignore. Without this the
/// redirect_slashes middleware uses the FIRST app's routes forever,
/// silently 404ing later apps' trailing-slash redirects.
static DECLARED_PATHS: std::sync::RwLock<Option<std::collections::HashSet<String>>> =
    std::sync::RwLock::new(None);

/// Paths that explicitly declare an OPTIONS route. The
/// ``non_preflight_options_middleware`` consults this set so that an
/// explicit ``@app.options("/p")`` handler is reached instead of being
/// short-circuited with 405. Path templates (``/items/{item_id}``) are
/// matched segment-by-segment.
static DECLARED_OPTIONS_PATHS: std::sync::RwLock<Option<std::collections::HashSet<String>>> =
    std::sync::RwLock::new(None);

/// Pass-through middleware for non-preflight OPTIONS. Tower-http's
/// ``CorsLayer`` intercepts *every* OPTIONS request with configured
/// ``allow_methods`` and returns 200 — FastAPI/Starlette only do that
/// for true preflights (request carrying both ``Origin`` and
/// ``Access-Control-Request-Method`` headers). Here we detect the
/// non-preflight case and strip the method to something the CORS layer
/// ignores, effectively routing through to the normal method handler.
async fn non_preflight_options_middleware_with_paths(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
    declared_options_paths: std::sync::Arc<std::collections::HashSet<String>>,
    _allow_methods_by_path: std::sync::Arc<std::collections::HashMap<String, Vec<String>>>,
    allow_methods_in_order: std::sync::Arc<Vec<(String, Vec<String>)>>,
) -> axum::response::Response {
    if req.method() == axum::http::Method::OPTIONS {
        let has_origin = req.headers().contains_key("origin");
        let has_acrm = req.headers().contains_key("access-control-request-method");
        if !(has_origin && has_acrm) {
            // If the user registered an explicit ``@app.options(...)``
            // route matching this path, let it through — the inner
            // router will dispatch to the user's handler.
            let path = req.uri().path();
            let has_explicit_opts = declared_options_paths
                .iter()
                .any(|tpl| options_path_matches(tpl, path));
            if !has_explicit_opts {
                // Walk registration order and use the FIRST template
                // whose pattern matches the incoming concrete path.
                // Matches Starlette / FastAPI's first-match-wins
                // semantics. Earlier code preferred the most-specific
                // template (fewest ``{}`` segments) — that matches
                // matchit's matcher but diverges from upstream when
                // a literal path is registered AFTER an overlapping
                // param path. Probe-confirmed: upstream returns the
                // first-registered route's methods.
                let first_match = allow_methods_in_order
                    .iter()
                    .find(|(tpl, _)| options_path_matches(tpl, path));
                let allow_header = first_match
                    .map(|(_, methods)| methods.join(", "))
                    .unwrap_or_else(String::new);
                if allow_header.is_empty() {
                    // Unknown path — let it through so the inner router
                    // can 404 it.
                    return next.run(req).await;
                }
                return axum::response::Response::builder()
                    .status(axum::http::StatusCode::METHOD_NOT_ALLOWED)
                    .header("content-type", "application/json")
                    .header("allow", allow_header)
                    .body(axum::body::Body::from(r#"{"detail":"Method Not Allowed"}"#))
                    .unwrap();
            }
        }
    }
    next.run(req).await
}

/// Starlette's CORSMiddleware responds to preflight requests with body
/// "OK" (Content-Type: text/plain; charset=utf-8). tower-http's
/// CorsLayer produces an empty-bodied 200. This middleware runs
/// *outside* the CorsLayer: if the incoming request looks like a CORS
/// preflight (OPTIONS + Origin + Access-Control-Request-Method) and
/// the downstream response carries the Access-Control-Allow-Origin
/// header (i.e. CORS accepted it), we rewrite the body to "OK" and
/// set Content-Type to text/plain.
/// Buffer the (already compressed) response body so we can emit a
/// Content-Length header. Starlette's GZipMiddleware does this
/// inherently; tower-http's CompressionLayer streams with
/// Transfer-Encoding: chunked. We only buffer when content-encoding is
/// gzip/deflate/br/zstd so unencoded streams (SSE, StreamingResponse)
/// are left alone.
async fn gzip_set_content_length_middleware(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let resp = next.run(req).await;
    let encoded = resp
        .headers()
        .get(axum::http::header::CONTENT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .map(|s| {
            let s = s.to_ascii_lowercase();
            s == "gzip" || s == "deflate" || s == "br" || s == "zstd"
        })
        .unwrap_or(false);
    if !encoded {
        return resp;
    }
    let (mut parts, body) = resp.into_parts();
    let bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(_) => {
            parts.headers.remove(axum::http::header::CONTENT_LENGTH);
            return axum::response::Response::from_parts(parts, axum::body::Body::empty());
        }
    };
    let len = bytes.len();
    parts.headers.insert(
        axum::http::header::CONTENT_LENGTH,
        axum::http::HeaderValue::from_str(&len.to_string()).unwrap(),
    );
    // Transfer-Encoding and Content-Length are mutually exclusive.
    parts.headers.remove(axum::http::header::TRANSFER_ENCODING);
    axum::response::Response::from_parts(parts, axum::body::Body::from(bytes))
}

async fn cors_preflight_ok_body_middleware(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let is_preflight = req.method() == axum::http::Method::OPTIONS
        && req.headers().contains_key("origin")
        && req.headers().contains_key("access-control-request-method");
    let resp = next.run(req).await;
    if !is_preflight {
        return resp;
    }
    if !resp.headers().contains_key("access-control-allow-origin") {
        return resp;
    }
    let (mut parts, _body) = resp.into_parts();
    parts.headers.insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    parts.headers.remove(axum::http::header::CONTENT_LENGTH);
    axum::response::Response::from_parts(parts, axum::body::Body::from("OK"))
}

fn options_path_matches(template: &str, concrete: &str) -> bool {
    // Starlette's ``{name:path}`` converter consumes multi-segment
    // tails. ``axum::matchit`` models the same as ``{*name}``. We
    // accept either form on the template side: when we see a
    // ``{_:path}`` or ``{*_}`` segment, every remaining concrete
    // segment is absorbed and the match succeeds.
    let t_segs: Vec<&str> = template.split('/').collect();
    let c_segs: Vec<&str> = concrete.split('/').collect();
    let mut ti = 0;
    let mut ci = 0;
    while ti < t_segs.len() {
        let t = t_segs[ti];
        // ``{*name}`` (axum form) or ``{name:path}`` (Starlette form)
        let is_catchall = (t.starts_with("{*") && t.ends_with('}'))
            || (t.starts_with('{') && t.ends_with('}') && t.contains(":path"));
        if is_catchall {
            // Must be the last template segment to absorb the tail.
            if ti != t_segs.len() - 1 {
                return false;
            }
            // At least one concrete segment (even empty) must remain,
            // and it must be non-empty so ``/files/`` doesn't match.
            if ci >= c_segs.len() {
                return false;
            }
            let remainder_empty = c_segs[ci..].iter().all(|s| s.is_empty());
            return !remainder_empty;
        }
        if ci >= c_segs.len() {
            return false;
        }
        let c = c_segs[ci];
        if t.starts_with('{') && t.ends_with('}') {
            if c.is_empty() {
                return false;
            }
        } else if t != c {
            return false;
        }
        ti += 1;
        ci += 1;
    }
    ci == c_segs.len()
}

/// Middleware that 307-redirects between `/foo` ↔ `/foo/` ONLY when the
/// alternate form matches a declared route. Matches Starlette's
/// `redirect_slashes=True` behaviour. Implemented as a pre-filter: we check
/// the declared-paths set before handing off to the inner router.
async fn slashes_redirect_middleware_with_paths(
    req: axum::http::Request<axum::body::Body>,
    next: axum::middleware::Next,
    declared_paths: std::sync::Arc<std::collections::HashSet<String>>,
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
    if declared_paths.is_empty() {
        return next.run(req).await;
    }
    // Path templates may carry ``{name}`` placeholders. Match them
    // segment-by-segment so ``/x/1`` → ``/x/{p}/`` comparison works.
    fn template_matches(template: &str, concrete: &str) -> bool {
        let t_segs: Vec<&str> = template.split('/').collect();
        let c_segs: Vec<&str> = concrete.split('/').collect();
        if t_segs.len() != c_segs.len() {
            return false;
        }
        for (t, c) in t_segs.iter().zip(c_segs.iter()) {
            if t.starts_with('{') && t.ends_with('}') {
                if c.is_empty() {
                    return false;
                }
                continue;
            }
            if t != c {
                return false;
            }
        }
        true
    }
    let matches_any = |p: &str| -> bool {
        declared_paths.contains(p)
            || declared_paths
                .iter()
                .any(|tpl| tpl.contains('{') && template_matches(tpl, p))
    };
    let known = matches_any(path);
    let alternate: String = if path.ends_with('/') {
        path[..path.len() - 1].to_string()
    } else {
        format!("{path}/")
    };
    let alt_known = matches_any(&alternate);
    if known || !alt_known {
        return next.run(req).await;
    }

    // Starlette/FA redirects ALL methods — using 307 (Temporary
    // Redirect) which preserves method + body on the re-request. Tests
    // like ``@app.post("/images/multiple/")`` hit via
    // ``client.post("/images/multiple")`` (no trailing) and expect 200
    // via the redirect.

    // Preserve query string in the redirect target
    let mut path_and_query = alternate;
    if let Some(q) = req.uri().query() {
        path_and_query.push('?');
        path_and_query.push_str(q);
    }

    // Starlette's trailing-slash redirect builds an ABSOLUTE URL using
    // request scheme + Host header. FA tests assert on
    // `https://example.com/items/` not just `/items/`.
    let host_hdr = req
        .headers()
        .get(axum::http::header::HOST)
        .and_then(|v| v.to_str().ok());
    let scheme_is_https = req
        .headers()
        .get("x-forwarded-proto")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.eq_ignore_ascii_case("https"))
        .unwrap_or(false)
        || req
            .uri()
            .scheme_str()
            .map(|s| s.eq_ignore_ascii_case("https"))
            .unwrap_or(false);
    let redirect_to = if let Some(host) = host_hdr {
        let scheme = if scheme_is_https { "https" } else { "http" };
        format!("{scheme}://{host}{path_and_query}")
    } else {
        path_and_query
    };

    axum::response::Response::builder()
        .status(axum::http::StatusCode::TEMPORARY_REDIRECT)
        .header("location", redirect_to)
        .body(axum::body::Body::empty())
        .unwrap()
}

/// Wait for either SIGINT (Ctrl-C) or SIGTERM. Many bench runners / process
/// supervisors send SIGTERM for orderly shutdown; the old handler only caught
/// SIGINT, so SIGTERM left fastapi-turbo hanging and the listener held its port
/// past the bench's cleanup phase (zombie processes blocking next-run startup).
/// Per-port programmatic shutdown registry. ``TestClient.__exit__``
/// and any other Python caller that wants to free a running server's
/// thread / port can call ``request_server_shutdown(port)`` from Python
/// to trip the ``Notify`` for that port. ``shutdown_signal_for_port``
/// races the signal-based shutdown against this notify.
static SERVER_SHUTDOWN_NOTIFIERS: std::sync::RwLock<
    Option<std::collections::HashMap<u16, std::sync::Arc<tokio::sync::Notify>>>,
> = std::sync::RwLock::new(None);

fn register_shutdown_notifier(port: u16) -> std::sync::Arc<tokio::sync::Notify> {
    let notify = std::sync::Arc::new(tokio::sync::Notify::new());
    let mut slot = SERVER_SHUTDOWN_NOTIFIERS.write().unwrap();
    let map = slot.get_or_insert_with(std::collections::HashMap::new);
    map.insert(port, notify.clone());
    notify
}

fn take_shutdown_notifier(port: u16) -> Option<std::sync::Arc<tokio::sync::Notify>> {
    let mut slot = SERVER_SHUTDOWN_NOTIFIERS.write().unwrap();
    slot.as_mut().and_then(|m| m.remove(&port))
}

/// Trigger a graceful shutdown of the server listening on ``port``.
/// Returns ``True`` if a running server was found and notified,
/// ``False`` otherwise. Called by the Python ``TestClient`` on
/// ``__exit__`` so long-running test suites don't leak ports/threads.
#[pyfunction]
pub fn request_server_shutdown(port: u16) -> bool {
    if let Some(n) = take_shutdown_notifier(port) {
        n.notify_waiters();
        true
    } else {
        false
    }
}

async fn shutdown_signal_for_port(port: u16) {
    let notify = register_shutdown_notifier(port);
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(_) => {
            // Fallback: only SIGINT or programmatic shutdown.
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = notify.notified() => {}
            }
            return;
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
        _ = notify.notified() => {}
    }
}

#[allow(dead_code)]
async fn shutdown_signal() {
    shutdown_signal_for_port(0).await;
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
                pyo3::exceptions::PyValueError::new_err("Middleware config must have a 'type' key")
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
                eprintln!("fastapi-turbo: unknown middleware type '{other}', skipping");
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
fn apply_middlewares(router: axum::Router, configs: &[MiddlewareConfig]) -> axum::Router {
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
                // Starlette's CORSMiddleware returns body "OK" with
                // Content-Type text/plain for preflight responses.
                // tower-http's CorsLayer produces empty 200s. Wrap the
                // outer response so preflights get "OK" body.
                app = app.layer(axum::middleware::from_fn(cors_preflight_ok_body_middleware));
            }
            MiddlewareConfig::Gzip => {
                app = app.layer(CompressionLayer::new());
                // Starlette's GZipMiddleware buffers the compressed body
                // and emits Content-Length. tower-http's CompressionLayer
                // streams the compressed body with Transfer-Encoding:
                // chunked. Buffer compressed responses so Content-Length
                // is present — FA tests assert on it.
                app = app.layer(axum::middleware::from_fn(
                    gzip_set_content_length_middleware,
                ));
            }
            MiddlewareConfig::TrustedHost { allowed_hosts } => {
                let hosts = allowed_hosts.clone();
                app = app.layer(axum::middleware::from_fn(
                    move |req: axum::http::Request<axum::body::Body>,
                          next: axum::middleware::Next| {
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
                            }
                            .to_lowercase();

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
                    },
                ));
            }
            MiddlewareConfig::HttpsRedirect => {
                app = app.layer(axum::middleware::from_fn(
                    |req: axum::http::Request<axum::body::Body>, next: axum::middleware::Next| {
                        async move {
                            // Check X-Forwarded-Proto header or URI scheme
                            let is_https = req
                                .headers()
                                .get("x-forwarded-proto")
                                .and_then(|v| v.to_str().ok())
                                .map(|s| s.eq_ignore_ascii_case("https"))
                                .unwrap_or_else(|| {
                                    req.uri()
                                        .scheme_str()
                                        .map(|s| s.eq_ignore_ascii_case("https"))
                                        .unwrap_or(false)
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
                            let path_and_query = req
                                .uri()
                                .path_and_query()
                                .map(|pq| pq.as_str())
                                .unwrap_or("/");
                            let redirect_url = format!("https://{host}{path_and_query}");

                            axum::http::Response::builder()
                                .status(axum::http::StatusCode::TEMPORARY_REDIRECT)
                                .header(axum::http::header::LOCATION, redirect_url)
                                .body(axum::body::Body::empty())
                                .unwrap()
                        }
                    },
                ));
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
