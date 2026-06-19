// Raw-Axum bench app — mirrors benchmarks/_bench_app.py endpoint-for-endpoint
// (plus /_ping, matching fastapi-turbo's built-in pure-Rust baseline) so the
// cross-framework comparison matrix is apples-to-apples. This is the
// "framework floor": idiomatic Axum with no Python in the loop.
//
// Build: cargo build --release
// Run:   PORT=8001 ./target/release/raw-axum-bench

use std::env;

use axum::{
    body::Body,
    extract::Path,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use futures_util::stream;
use serde::{Deserialize, Serialize};
use serde_json::json;

// ── streaming contract ──────────────────────────────────────────────
// Exactly 10 chunks. Each chunk is a fixed 64-byte line:
//   "chunk-{i}: " (9 bytes for single-digit i) + 54 '=' pad + "\n"
// Total body = 640 bytes. Content-Type: text/plain; charset=utf-8.
// NO artificial per-chunk delay — this measures framework streaming
// OVERHEAD, not I/O. MUST stay byte-identical across all 5 servers.
const STREAM_CHUNKS: usize = 10;

fn stream_chunk(i: usize) -> Vec<u8> {
    let mut s = format!("chunk-{i}: ");
    s.push_str(&"=".repeat(54));
    s.push('\n');
    debug_assert_eq!(s.len(), 64, "chunk {i} not 64 bytes");
    s.into_bytes()
}

// ── simulated 2-level dependency chain (raw axum has no DI) ──
fn get_db() -> bool {
    true
}

fn get_user(_db: bool, _authorization: &str) -> &'static str {
    "alice"
}

#[derive(Deserialize)]
struct Item {
    sku: String,
    qty: i64,
    #[serde(default)]
    tags: Vec<String>,
}

#[derive(Serialize)]
struct ItemResp {
    ok: bool,
    sku: String,
    qty: i64,
    tag_count: usize,
}

async fn ping() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "application/json")],
        r#"{"ping":"pong"}"#,
    )
}

async fn hello() -> impl IntoResponse {
    Json(json!({"message": "hello"}))
}

async fn path_param(Path(id): Path<i64>) -> impl IntoResponse {
    Json(json!({"id": id, "squared": id * id}))
}

async fn headers(hdrs: HeaderMap) -> impl IntoResponse {
    let ua = hdrs
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("unknown");
    let req_id = hdrs.get("x-request-id").and_then(|v| v.to_str().ok());
    Json(json!({"ua": ua, "req_id": req_id}))
}

async fn with_deps(hdrs: HeaderMap) -> impl IntoResponse {
    let auth = hdrs
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("tok-demo");
    let db = get_db();
    let user = get_user(db, auth);
    Json(json!({"user": user, "db": db}))
}

async fn list_items() -> impl IntoResponse {
    let items: Vec<_> = (0..20)
        .map(|i| json!({"id": i, "name": format!("item-{i}")}))
        .collect();
    Json(json!({"items": items}))
}

async fn create_item(payload: Result<Json<Item>, axum::extract::rejection::JsonRejection>) -> Response {
    match payload {
        Ok(Json(item)) => Json(ItemResp {
            ok: true,
            sku: item.sku,
            qty: item.qty,
            tag_count: item.tags.len(),
        })
        .into_response(),
        Err(_) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": "invalid body"})),
        )
            .into_response(),
    }
}

// 10-chunk streaming body, idiomatic Axum: a stream of Result<Bytes>
// items wrapped into a Body. No delay between chunks.
async fn stream_handler() -> impl IntoResponse {
    let s = stream::iter(
        (0..STREAM_CHUNKS).map(|i| Ok::<_, std::io::Error>(stream_chunk(i))),
    );
    (
        [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
        Body::from_stream(s),
    )
}

#[tokio::main]
async fn main() {
    let app = Router::new()
        .route("/_ping", get(ping))
        .route("/hello", get(hello))
        .route("/path/{id}", get(path_param))
        .route("/headers", get(headers))
        .route("/with-deps", get(with_deps))
        .route("/list", get(list_items))
        .route("/items", post(create_item))
        .route("/stream", get(stream_handler));

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8001);
    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    eprintln!("raw-axum bench app on http://{addr}");
    axum::serve(listener, app).await.unwrap();
}
