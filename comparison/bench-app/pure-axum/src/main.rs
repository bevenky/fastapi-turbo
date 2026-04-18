//! Pure-axum file handling benchmark.
//!
//! Mirrors the endpoints of `files_fastapi_rs.py` / `files_go_gin.go` /
//! `files_fastify.js` exactly so we can measure the absolute Rust ceiling
//! for our framework overhead comparison.

use axum::{
    body::Body,
    extract::{Multipart, Path, State, ws::{Message, WebSocket, WebSocketUpgrade}},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};
use std::sync::Mutex;
use std::collections::HashMap;
use tokio::net::TcpListener;
use tower_http::compression::CompressionLayer;

#[derive(Clone)]
struct AppState {
    tmp_dir: Arc<std::path::PathBuf>,
    big_items: Arc<Vec<Item>>,
    static_cache: Arc<Mutex<HashMap<String, CachedFile>>>,
    tpl_env: Arc<minijinja::Environment<'static>>,
}

#[derive(Serialize, Clone)]
struct Item {
    id: usize,
    name: String,
    desc: String,
}

#[derive(Clone)]
struct CachedFile {
    bytes: bytes::Bytes,
    content_type: &'static str,
    validated_at: Instant,
    mtime: SystemTime,
}
const STATIC_TTL: Duration = Duration::from_secs(1);

fn mime_for(name: &str) -> &'static str {
    let ext = name.rsplit('.').next().unwrap_or("");
    match ext.to_ascii_lowercase().as_str() {
        "html" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "application/javascript; charset=utf-8",
        "json" => "application/json",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

async fn health() -> &'static str {
    "ok"
}

#[derive(Serialize)]
struct UploadResp {
    filename: String,
    size: usize,
}

async fn upload(mut parts: Multipart) -> Response {
    while let Ok(Some(field)) = parts.next_field().await {
        let filename = field.file_name().unwrap_or("").to_string();
        if let Ok(data) = field.bytes().await {
            let r = UploadResp { filename, size: data.len() };
            let body = serde_json::to_vec(&r).unwrap();
            return (
                StatusCode::OK,
                [("content-type", "application/json")],
                body,
            )
                .into_response();
        }
    }
    (StatusCode::BAD_REQUEST, "no file").into_response()
}

async fn download(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Response {
    let path = state.tmp_dir.join(&name);
    match std::fs::read(&path) {
        Ok(data) => {
            let len = data.len();
            let ct = mime_for(&name);
            Response::builder()
                .status(StatusCode::OK)
                .header("content-type", ct)
                .header("content-length", len)
                .header("accept-ranges", "bytes")
                .body(Body::from(data))
                .unwrap()
        }
        Err(_) => (StatusCode::NOT_FOUND, "not found").into_response(),
    }
}

async fn static_serve(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Response {
    let path = state.tmp_dir.join(&name);
    // Cache hot path
    let cached = {
        let g = state.static_cache.lock().unwrap();
        g.get(&name).cloned()
    };
    if let Some(cf) = cached {
        if cf.validated_at.elapsed() < STATIC_TTL {
            return Response::builder()
                .status(StatusCode::OK)
                .header("content-type", cf.content_type)
                .header("content-length", cf.bytes.len())
                .body(Body::from(cf.bytes))
                .unwrap();
        }
        if let Ok(meta) = std::fs::metadata(&path) {
            if let Ok(mt) = meta.modified() {
                if mt == cf.mtime {
                    let mut g = state.static_cache.lock().unwrap();
                    if let Some(e) = g.get_mut(&name) {
                        e.validated_at = Instant::now();
                    }
                    return Response::builder()
                        .status(StatusCode::OK)
                        .header("content-type", cf.content_type)
                        .header("content-length", cf.bytes.len())
                        .body(Body::from(cf.bytes))
                        .unwrap();
                }
            }
        }
    }
    // Miss — read + cache
    match std::fs::read(&path) {
        Ok(data) => {
            let ct = mime_for(&name);
            let mtime = std::fs::metadata(&path)
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            let bytes = bytes::Bytes::from(data);
            let mut g = state.static_cache.lock().unwrap();
            g.insert(name.clone(), CachedFile {
                bytes: bytes.clone(),
                content_type: ct,
                validated_at: Instant::now(),
                mtime,
            });
            drop(g);
            Response::builder()
                .status(StatusCode::OK)
                .header("content-type", ct)
                .header("content-length", bytes.len())
                .body(Body::from(bytes))
                .unwrap()
        }
        Err(_) => (StatusCode::NOT_FOUND, "not found").into_response(),
    }
}

async fn ws_text(ws: WebSocketUpgrade) -> Response {
    ws.on_upgrade(|socket: WebSocket| async move {
        let (mut tx, mut rx) = socket.split();
        while let Some(msg) = rx.next().await {
            match msg {
                Ok(Message::Text(t)) => {
                    if tx.send(Message::Text(t)).await.is_err() {
                        break;
                    }
                }
                Ok(Message::Binary(b)) => {
                    if tx.send(Message::Binary(b)).await.is_err() {
                        break;
                    }
                }
                Ok(Message::Close(_)) | Err(_) => break,
                _ => {}
            }
        }
    })
}

async fn ws_bytes(ws: WebSocketUpgrade) -> Response {
    ws_text(ws).await
}

async fn render_tpl(State(state): State<AppState>) -> Response {
    let items: Vec<_> = (0..20).map(|i| {
        serde_json::json!({
            "name": format!("Item {i}"),
            "price": 9.99 + (i as f64),
            "on_sale": i % 3 == 0,
        })
    }).collect();
    let ctx = serde_json::json!({
        "title": "Products",
        "name": "Alice",
        "user": {"name": "Alice", "email": "alice@example.com"},
        "items": items,
    });
    render_named(&state, "page.html", ctx)
}

async fn render_tpl_large(State(state): State<AppState>) -> Response {
    let groups: Vec<_> = (0..5).map(|g| {
        let items: Vec<_> = (0..20).map(|i| {
            serde_json::json!({
                "name": format!("SKU-{g}-{i}"),
                "value": g * 100 + i,
                "in_stock": (g + i) % 3 != 0,
                "qty": g * 10 + i,
            })
        }).collect();
        serde_json::json!({"name": format!("Group {g}"), "items": items})
    }).collect();
    let ctx = serde_json::json!({
        "title": "Inventory",
        "heading": "Warehouse snapshot",
        "groups": groups,
    });
    render_named(&state, "large.html", ctx)
}

fn render_named(state: &AppState, name: &str, ctx: serde_json::Value) -> Response {
    let tmpl = match state.tpl_env.get_template(name) {
        Ok(t) => t,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "no tmpl").into_response(),
    };
    let out = match tmpl.render(ctx) {
        Ok(s) => s,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "render err").into_response(),
    };
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/html; charset=utf-8")
        .body(Body::from(out))
        .unwrap()
}

async fn json_big(State(state): State<AppState>) -> Response {
    let bytes = serde_json::to_vec(&*state.big_items).unwrap();
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Body::from(bytes))
        .unwrap()
}

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let tmp = tempfile::tempdir_in(std::env::temp_dir()).unwrap();
    let tmp_path = tmp.into_path();
    for (name, content) in &[
        ("small.txt", "hello world\n".repeat(10).into_bytes()),
        ("medium.bin", vec![b'x'; 64 * 1024]),
        ("large.bin", vec![b'x'; 1024 * 1024]),
        ("style.css", "body{color:red;}".repeat(100).into_bytes()),
    ] {
        std::fs::write(tmp_path.join(name), content).unwrap();
    }

    let big_items: Vec<Item> = (0..200)
        .map(|i| Item {
            id: i,
            name: format!("item-{i}"),
            desc: "lorem ipsum dolor sit amet ".repeat(4),
        })
        .collect();

    // minijinja env pointing at the same templates dir the Python bench uses.
    let mut tpl_env = minijinja::Environment::new();
    let tpl_dir = std::env::current_dir()
        .unwrap()
        .join("templates");
    tpl_env.set_loader(move |name| {
        let full = tpl_dir.join(name);
        match std::fs::read_to_string(&full) {
            Ok(s) => Ok(Some(s)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(minijinja::Error::new(
                minijinja::ErrorKind::TemplateNotFound,
                e.to_string(),
            )),
        }
    });

    let state = AppState {
        tmp_dir: Arc::new(tmp_path),
        big_items: Arc::new(big_items),
        static_cache: Arc::new(Mutex::new(HashMap::new())),
        tpl_env: Arc::new(tpl_env),
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/upload", post(upload))
        .route("/download/{name}", get(download))
        .route("/static/{name}", get(static_serve))
        .route("/json-big", get(json_big))
        .route("/ws-text", get(ws_text))
        .route("/ws-bytes", get(ws_bytes))
        .route("/tpl", get(render_tpl))
        .route("/tpl-large", get(render_tpl_large))
        .layer(CompressionLayer::new())
        .with_state(state);

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8400);
    let addr = format!("127.0.0.1:{port}");
    let listener = TcpListener::bind(&addr).await.unwrap();
    println!("pure-axum on {addr}");
    axum::serve(listener, app).await.unwrap();
}
