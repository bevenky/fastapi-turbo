use axum::routing::get;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tower_http::compression::CompressionLayer;
use tower_http::cors::{Any as CorsAny, CorsLayer};
use tower_http::services::ServeDir;

use crate::router::{build_router, RouteInfo};

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
#[pyo3(signature = (routes, host, port, middlewares=vec![], openapi_json=None, docs_url=None, redoc_url=None, openapi_url=None, static_mounts=vec![], root_path=None))]
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
) -> PyResult<()> {
    // Parse middleware config while we still have the GIL
    let mw_configs = parse_middleware_configs(py, &middlewares)?;

    // Release the GIL for the entire duration of the blocking server run.
    py.detach(|| {
        let rt = Runtime::new().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Failed to create tokio runtime: {e}"
            ))
        })?;

        rt.block_on(async move {
            let mut router = build_router(routes);

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

            // Mount static file directories
            for (prefix, directory) in &static_mounts {
                router = router.nest_service(prefix, ServeDir::new(directory));
            }

            // Nest the entire app under root_path if specified (reverse proxy support)
            let router = if let Some(ref prefix) = root_path {
                if !prefix.is_empty() && prefix != "/" {
                    axum::Router::new().nest(prefix, router)
                } else {
                    router
                }
            } else {
                router
            };

            let app = apply_middlewares(router, &mw_configs);

            let addr = format!("{host}:{port}");
            let listener = TcpListener::bind(&addr).await.map_err(|e| {
                pyo3::exceptions::PyOSError::new_err(format!(
                    "Failed to bind to {addr}: {e}"
                ))
            })?;

            println!("fastapi-rs running on http://{addr}");

            axum::serve(listener, app)
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
        let dict = mw.downcast::<PyDict>()?;

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

    // Origins
    if allow_origins.iter().any(|o| o == "*") {
        cors = cors.allow_origin(CorsAny);
    } else if !allow_origins.is_empty() {
        let origins: Vec<http::HeaderValue> = allow_origins
            .iter()
            .filter_map(|o| o.parse().ok())
            .collect();
        cors = cors.allow_origin(origins);
    }

    // Methods
    if allow_methods.iter().any(|m| m == "*") {
        cors = cors.allow_methods(CorsAny);
    } else if !allow_methods.is_empty() {
        let methods: Vec<Method> = allow_methods
            .iter()
            .filter_map(|m| Method::from_str(m).ok())
            .collect();
        cors = cors.allow_methods(methods);
    }

    // Headers
    if allow_headers.iter().any(|h| h == "*") {
        cors = cors.allow_headers(CorsAny);
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
