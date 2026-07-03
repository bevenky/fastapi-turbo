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
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, Query, State,
    },
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, patch, post, put},
    Json, Router,
};
use deadpool_postgres::{Manager, ManagerConfig, Pool as PgPool, RecyclingMethod};
use deadpool_redis::{Config as RedisConfig, Pool as RedisPool, PoolConfig, Runtime};
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

// ── cross-framework DB matrix (app_db.py) models ─────────────────────
#[derive(Deserialize)]
struct CommitQuery {
    #[serde(default = "default_commit")]
    commit: bool,
}

fn default_commit() -> bool {
    true
}

#[derive(Serialize)]
struct WroteResp {
    ok: bool,
    committed: bool,
}

#[derive(Serialize)]
struct PipeResp {
    ok: bool,
    n: i64,
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
    // prepare_cached: a bare &str re-prepares on EVERY call (2 wire round
    // trips); the cached statement makes reads 1 RTT, matching pgx.
    let stmt = match client
        .prepare_cached("SELECT id, sku, name, qty FROM items WHERE id = $1")
        .await
    {
        Ok(s) => s,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let row = client.query_opt(&stmt, &[&pk]).await;
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
    let stmt = match client
        .prepare_cached("SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1")
        .await
    {
        Ok(s) => s,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let rows = client.query(&stmt, &[&q.limit]).await;
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

// ── cross-framework Postgres matrix (app_db.py /pg/{op}/{sync|async}) ──
// These servers have ONE native driver and no sync/async split, so the
// /…/sync and /…/async routes share the same handler code.
async fn pg_select_one(State(state): State<Arc<AppState>>) -> Response {
    let client = match state.pg.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    // items.id is int4 -> bind WHERE id=$1 as i32.
    let id: i32 = 5;
    let stmt = match client
        .prepare_cached("SELECT id, sku, name, qty FROM items WHERE id = $1")
        .await
    {
        Ok(s) => s,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let row = client.query_opt(&stmt, &[&id]).await;
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

async fn pg_select_list(State(state): State<Arc<AppState>>) -> Response {
    let client = match state.pg.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let limit: i64 = 10;
    let stmt = match client
        .prepare_cached("SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1")
        .await
    {
        Ok(s) => s,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let rows = client.query(&stmt, &[&limit]).await;
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

// Run a single write inside an explicit transaction; commit or roll back per
// the ?commit= flag so commit=false leaves the DB unchanged (repeatable).
async fn pg_write(state: &Arc<AppState>, sql: &str, params: &[&(dyn tokio_postgres::types::ToSql + Sync)], commit: bool) -> Response {
    let mut client = match state.pg.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let txn = match client.transaction().await {
        Ok(t) => t,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if txn.execute(sql, params).await.is_err() {
        let _ = txn.rollback().await;
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let res = if commit { txn.commit().await } else { txn.rollback().await };
    match res {
        Ok(_) => Json(WroteResp { ok: true, committed: commit }).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn pg_insert(State(state): State<Arc<AppState>>, Query(q): Query<CommitQuery>) -> Response {
    pg_write(&state, "INSERT INTO bench_writes (val) VALUES ($1)", &[&"x"], q.commit).await
}

async fn pg_update(State(state): State<Arc<AppState>>, Query(q): Query<CommitQuery>) -> Response {
    pg_write(&state, "UPDATE bench_writes SET val = $1 WHERE id = 5", &[&"y"], q.commit).await
}

async fn pg_delete(State(state): State<Arc<AppState>>, Query(q): Query<CommitQuery>) -> Response {
    let del_id: i32 = 999_999_999;
    pg_write(&state, "DELETE FROM bench_writes WHERE id = $1", &[&del_id], q.commit).await
}

// ── cross-framework Redis matrix (app_db.py /redis/{sync|async}/{op}) ──
const RKEY: &str = "bench:item";
const RWKEY: &str = "bench:wkey";
const RVAL: &str = "bench-value";

async fn redis_get_x(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let val: Option<String> = conn.get(RKEY).await.ok().flatten();
    match val {
        Some(v) => ([(header::CONTENT_TYPE, "application/json")], v).into_response(),
        None => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn redis_set_x(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let r: redis::RedisResult<()> = conn.set(RWKEY, RVAL).await;
    match r {
        Ok(_) => Json(OkResp { ok: true }).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn redis_set_durable(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let r: redis::RedisResult<()> = conn.set(RWKEY, RVAL).await;
    if r.is_err() {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    // durability: force an AOF fsync via WAITAOF (local fsync, 0 replicas).
    // Ignore the error if AOF is off.
    let _: redis::RedisResult<redis::Value> = redis::cmd("WAITAOF")
        .arg(1)
        .arg(0)
        .arg(100)
        .query_async(&mut conn)
        .await;
    Json(OkResp { ok: true }).into_response()
}

async fn redis_pipeline(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let mut pipe = redis::pipe(); // NON-transactional
    for i in 0..10 {
        pipe.set(format!("{RWKEY}:{i}"), RVAL).ignore();
    }
    let r: redis::RedisResult<()> = pipe.query_async(&mut conn).await;
    match r {
        Ok(_) => Json(PipeResp { ok: true, n: 10 }).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn redis_multi(State(state): State<Arc<AppState>>) -> Response {
    let mut conn = match state.redis.get().await {
        Ok(c) => c,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let mut pipe = redis::pipe();
    pipe.atomic(); // MULTI/EXEC
    for i in 0..10 {
        pipe.set(format!("{RWKEY}:{i}"), RVAL).ignore();
    }
    let r: redis::RedisResult<()> = pipe.query_async(&mut conn).await;
    match r {
        Ok(_) => Json(PipeResp { ok: true, n: 10 }).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

// ── WebSocket echo (contract: receive text -> echo SAME text back) ────
async fn ws_upgrade(ws: WebSocketUpgrade) -> Response {
    ws.on_upgrade(ws_echo)
}

async fn ws_echo(mut socket: WebSocket) {
    while let Some(Ok(msg)) = socket.recv().await {
        match msg {
            // echo text as text, binary as binary — payload verbatim
            Message::Text(t) => {
                if socket.send(Message::Text(t)).await.is_err() {
                    return;
                }
            }
            Message::Binary(b) => {
                if socket.send(Message::Binary(b)).await.is_err() {
                    return;
                }
            }
            Message::Close(_) => return, // axum completes the close handshake
            _ => {}                      // ping/pong handled automatically
        }
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
    let mut cfg = RedisConfig::from_url("redis://127.0.0.1:6379");
    // deadpool's default max_size is cpu_cores*2 (36 here) — BELOW the bench
    // concurrency (64). That caps in-flight redis commands and binds hard on
    // the fsync-bound set_durable row: Redis group-commits fsyncs across all
    // in-flight writers (~66 rps/writer floor on this disk), so a 36-conn cap
    // pinned raw-axum at ~2.4k rps while every other framework had >=64
    // commands in flight. Size the pool above bench concurrency so the row
    // measures Redis, not the pool.
    cfg.pool = Some(PoolConfig::new(128));
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
        // cross-framework Postgres matrix (app_db.py) — sync == async handler
        .route("/pg/select_one/sync", get(pg_select_one))
        .route("/pg/select_one/async", get(pg_select_one))
        .route("/pg/select_list/sync", get(pg_select_list))
        .route("/pg/select_list/async", get(pg_select_list))
        .route("/pg/insert/sync", post(pg_insert))
        .route("/pg/insert/async", post(pg_insert))
        .route("/pg/update/sync", post(pg_update))
        .route("/pg/update/async", post(pg_update))
        .route("/pg/delete/sync", post(pg_delete))
        .route("/pg/delete/async", post(pg_delete))
        // cross-framework Redis matrix (app_db.py) — sync == async handler
        .route("/redis/sync/get", get(redis_get_x))
        .route("/redis/async/get", get(redis_get_x))
        .route("/redis/sync/set", post(redis_set_x))
        .route("/redis/async/set", post(redis_set_x))
        .route("/redis/sync/set_durable", post(redis_set_durable))
        .route("/redis/async/set_durable", post(redis_set_durable))
        .route("/redis/sync/pipeline", post(redis_pipeline))
        .route("/redis/async/pipeline", post(redis_pipeline))
        .route("/redis/sync/multi", post(redis_multi))
        .route("/redis/async/multi", post(redis_multi))
        // WebSocket echo
        .route("/ws", get(ws_upgrade))
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
