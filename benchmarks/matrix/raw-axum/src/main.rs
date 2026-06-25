// Raw-Axum reference server for the cross-framework bench matrix. Mirrors
// benchmarks/matrix/app.py (the CONTRACT) endpoint-for-endpoint with
// semantically-identical, byte-comparable JSON. Key ORDER follows the
// Python module via ordered #[derive(Serialize)] structs (serde preserves
// field declaration order). JSON uses only ints + strings (no floats).
//
// Backends (shared, already running):
//   Postgres: host=127.0.0.1 port=5432 dbname=fastapi_turbo_bench user=venky
//             (deadpool-postgres pool, min 4 / max 8)
//   Redis:    redis://127.0.0.1:6379 (deadpool-redis pool)
//
// Build: PATH="$HOME/.cargo/bin:$PATH" cargo build --release
// Run:   PORT=8005 ./target/release/raw-axum-matrix

use std::env;
use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, patch, post, put},
    Json, Router,
};
use deadpool_postgres::{Manager, ManagerConfig, Pool as PgPool, RecyclingMethod};
use deadpool_redis::{Config as RedisConfig, Pool as RedisPool, Runtime};
use futures_util::stream;
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;

// ── streaming contract: 10 chunks, each a fixed 64-byte line ──────────
//   "chunk-{i}: " + 54 '=' + "\n"  (64 bytes for single-digit i)
// Content-Type: text/plain; charset=utf-8. No artificial delay.
const STREAM_CHUNKS: usize = 10;
const STREAM_CT: &str = "text/plain; charset=utf-8";

fn stream_chunk(i: usize) -> Vec<u8> {
    let mut s = format!("chunk-{i}: ");
    s.push_str(&"=".repeat(54));
    s.push('\n');
    debug_assert_eq!(s.len(), 64, "chunk {i} not 64 bytes");
    s.into_bytes()
}

// ── XML payloads (exact bytes) ────────────────────────────────────────
const XML_SMALL: &str =
    r#"<?xml version="1.0" encoding="UTF-8"?><item><id>1</id><name>item-1</name></item>"#;

fn xml_large() -> String {
    let mut s = String::with_capacity(46000);
    s.push_str(r#"<?xml version="1.0" encoding="UTF-8"?><items>"#);
    for i in 0..1000 {
        s.push_str(&format!("<item><id>{i}</id><name>item-{i}</name></item>"));
    }
    s.push_str("</items>");
    s
}

// ── shared state: connection pools ────────────────────────────────────
struct AppState {
    pg: PgPool,
    redis: RedisPool,
    xml_large: String,
}

// ── request / response models (field order == contract order) ─────────
#[derive(Deserialize)]
struct Item {
    sku: String,
    qty: i64,
    #[serde(default)]
    tags: Vec<String>,
}

#[derive(Deserialize)]
struct PatchItem {
    qty: i64,
}

#[derive(Serialize)]
struct Hello {
    message: &'static str,
}

#[derive(Serialize)]
struct LargeEntry {
    id: i64,
    name: String,
}

#[derive(Serialize)]
struct LargeList {
    items: Vec<LargeEntry>,
}

#[derive(Serialize)]
struct CreateResp {
    ok: bool,
    sku: String,
    qty: i64,
    tag_count: usize,
}

#[derive(Serialize)]
struct PutResp {
    ok: bool,
    id: i64,
    sku: String,
    qty: i64,
    tag_count: usize,
}

#[derive(Serialize)]
struct PatchResp {
    ok: bool,
    id: i64,
    qty: i64,
}

#[derive(Serialize)]
struct DeleteResp {
    ok: bool,
    deleted: i64,
}

#[derive(Serialize)]
struct OkResp {
    ok: bool,
}

#[derive(Serialize)]
struct PgItem {
    // items.id and items.qty are Postgres `integer` (int4) -> Rust i32.
    id: i32,
    sku: String,
    name: String,
    qty: i32,
}

#[derive(Serialize)]
struct PgItemList {
    items: Vec<PgItem>,
}

#[derive(Deserialize)]
struct ItemsQuery {
    #[serde(default = "default_limit")]
    limit: i64,
}

fn default_limit() -> i64 {
    10
}

// ── plain JSON: simple ────────────────────────────────────────────────
async fn hello() -> impl IntoResponse {
    Json(Hello { message: "hello" })
}

// ── plain JSON: large ─────────────────────────────────────────────────
fn large_list() -> LargeList {
    LargeList {
        items: (0..1000)
            .map(|i| LargeEntry {
                id: i,
                name: format!("item-{i}"),
            })
            .collect(),
    }
}

async fn json_large() -> impl IntoResponse {
    Json(large_list())
}

// ── body methods: POST / PUT / PATCH / DELETE ─────────────────────────
async fn create_item(
    payload: Result<Json<Item>, axum::extract::rejection::JsonRejection>,
) -> Response {
    match payload {
        Ok(Json(item)) => Json(CreateResp {
            ok: true,
            sku: item.sku,
            qty: item.qty,
            tag_count: item.tags.len(),
        })
        .into_response(),
        Err(_) => unprocessable(),
    }
}

async fn put_item(
    Path(item_id): Path<i64>,
    payload: Result<Json<Item>, axum::extract::rejection::JsonRejection>,
) -> Response {
    match payload {
        Ok(Json(item)) => Json(PutResp {
            ok: true,
            id: item_id,
            sku: item.sku,
            qty: item.qty,
            tag_count: item.tags.len(),
        })
        .into_response(),
        Err(_) => unprocessable(),
    }
}

async fn patch_item(
    Path(item_id): Path<i64>,
    payload: Result<Json<PatchItem>, axum::extract::rejection::JsonRejection>,
) -> Response {
    match payload {
        Ok(Json(p)) => Json(PatchResp {
            ok: true,
            id: item_id,
            qty: p.qty,
        })
        .into_response(),
        Err(_) => unprocessable(),
    }
}

async fn delete_item(Path(item_id): Path<i64>) -> impl IntoResponse {
    Json(DeleteResp {
        ok: true,
        deleted: item_id,
    })
}

fn unprocessable() -> Response {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(serde_json::json!({"detail": "invalid body"})),
    )
        .into_response()
}

// ── XML ───────────────────────────────────────────────────────────────
async fn xml_small() -> impl IntoResponse {
    ([(header::CONTENT_TYPE, "application/xml")], XML_SMALL)
}

async fn xml_large_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "application/xml")],
        state.xml_large.clone(),
    )
}

// ── streaming ─────────────────────────────────────────────────────────
async fn stream_handler() -> impl IntoResponse {
    let s = stream::iter(
        (0..STREAM_CHUNKS).map(|i| Ok::<_, std::io::Error>(stream_chunk(i))),
    );
    (
        [(header::CONTENT_TYPE, STREAM_CT)],
        Body::from_stream(s),
    )
}

// ── Redis ─────────────────────────────────────────────────────────────
async fn redis_get(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let val: Option<String> = conn.get("bench:item").await.ok().flatten();
    match val {
        Some(v) => ([(header::CONTENT_TYPE, "application/json")], v).into_response(),
        None => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn redis_set(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let r: redis::RedisResult<()> = conn.set("bench:wkey", "bench-value").await;
    match r {
        Ok(_) => Json(OkResp { ok: true }).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

// ── Postgres ──────────────────────────────────────────────────────────
async fn pg_item(
    State(state): State<Arc<AppState>>,
    Path(item_id): Path<i64>,
) -> Response {
    let client = match state.pg.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    // items.id is int4; bind an i32 so the parameter type matches the column.
    let pk = item_id as i32;
    let row = client
        .query_opt(
            "SELECT id, sku, name, qty FROM items WHERE id = $1",
            &[&pk],
        )
        .await;
    match row {
        Ok(Some(r)) => Json(PgItem {
            id: r.get(0),
            sku: r.get(1),
            name: r.get(2),
            qty: r.get(3),
        })
        .into_response(),
        Ok(None) => StatusCode::NOT_FOUND.into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn pg_items(
    State(state): State<Arc<AppState>>,
    Query(q): Query<ItemsQuery>,
) -> Response {
    let client = match state.pg.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let rows = client
        .query(
            "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1",
            &[&q.limit],
        )
        .await;
    match rows {
        Ok(rows) => {
            let items = rows
                .iter()
                .map(|r| PgItem {
                    id: r.get(0),
                    sku: r.get(1),
                    name: r.get(2),
                    qty: r.get(3),
                })
                .collect();
            Json(PgItemList { items }).into_response()
        }
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

// ── baseline ──────────────────────────────────────────────────────────
async fn ping() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "application/json")],
        r#"{"ping":"pong"}"#,
    )
}

fn build_pg_pool() -> PgPool {
    let mut pg_cfg = tokio_postgres::Config::new();
    pg_cfg
        .host("127.0.0.1")
        .port(5432)
        .dbname("fastapi_turbo_bench")
        .user("venky");
    let mgr = Manager::from_config(
        pg_cfg,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );
    PgPool::builder(mgr)
        .max_size(8)
        .build()
        .expect("build pg pool")
}

fn build_redis_pool() -> RedisPool {
    let cfg = RedisConfig::from_url("redis://127.0.0.1:6379");
    cfg.create_pool(Some(Runtime::Tokio1))
        .expect("build redis pool")
}

#[tokio::main]
async fn main() {
    let pg = build_pg_pool();
    let redis = build_redis_pool();

    // Warm the PG pool to min 4 connections (deadpool has no min_idle; pre-acquire).
    {
        let mut held = Vec::new();
        for _ in 0..4 {
            if let Ok(c) = pg.get().await {
                held.push(c);
            }
        }
        // drop -> returned to pool
    }

    let state = Arc::new(AppState {
        pg,
        redis,
        xml_large: xml_large(),
    });

    let app = Router::new()
        // simple JSON
        .route("/hello", get(hello))
        .route("/async/hello", get(hello))
        // large JSON
        .route("/json/large", get(json_large))
        .route("/async/json/large", get(json_large))
        // body methods
        .route("/items", post(create_item))
        .route("/async/items", post(create_item))
        .route("/items/{item_id}", put(put_item))
        .route("/items/{item_id}", patch(patch_item))
        .route("/items/{item_id}", delete(delete_item))
        // XML
        .route("/xml/small", get(xml_small))
        .route("/xml/large", get(xml_large_handler))
        // streaming
        .route("/stream-sync", get(stream_handler))
        .route("/stream-async", get(stream_handler))
        .route("/stream-await", get(stream_handler))
        // Redis
        .route("/redis/get/sync", get(redis_get))
        .route("/redis/get/async", get(redis_get))
        .route("/redis/set/sync", post(redis_set))
        .route("/redis/set/async", post(redis_set))
        // Postgres
        .route("/pg/item/{item_id}/sync", get(pg_item))
        .route("/pg/item/{item_id}/async", get(pg_item))
        .route("/pg/items/sync", get(pg_items))
        .route("/pg/items/async", get(pg_items))
        // baseline
        .route("/_ping", get(ping))
        .with_state(state);

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8005);
    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    eprintln!("raw-axum-matrix bench app on http://{addr}");
    axum::serve(listener, app).await.unwrap();
}
