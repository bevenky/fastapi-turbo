use axum::{extract::State, response::IntoResponse, routing::{get, post}, Json, Router};
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use std::sync::Arc;

#[derive(Clone)]
struct AppState { redis: Arc<tokio::sync::Mutex<ConnectionManager>> }

async fn health() -> impl IntoResponse {
    Json(serde_json::json!({"status": "ok"}))
}

async fn cache_get(State(s): State<AppState>) -> impl IntoResponse {
    let mut conn = s.redis.lock().await;
    let v: Option<String> = conn.get("bench:key").await.unwrap_or(None);
    Json(serde_json::json!({"v": v}))
}

async fn cache_set(State(s): State<AppState>) -> impl IntoResponse {
    let mut conn = s.redis.lock().await;
    let _: () = conn.set("bench:key", "updated").await.unwrap_or(());
    Json(serde_json::json!({"ok": true}))
}

#[tokio::main]
async fn main() {
    let client = redis::Client::open("redis://127.0.0.1:6379/").unwrap();
    let mut mgr = ConnectionManager::new(client).await.unwrap();
    let _: () = mgr.set("bench:key", "hello-world").await.unwrap_or(());
    let state = AppState { redis: Arc::new(tokio::sync::Mutex::new(mgr)) };

    let app = Router::new()
        .route("/health", get(health))
        .route("/cache/get", get(cache_get))
        .route("/cache/set", post(cache_set))
        .with_state(state);

    let port = std::env::var("PORT").unwrap_or_else(|_| "19043".to_string());
    let addr = format!("127.0.0.1:{port}");
    eprintln!("Redis Axum on {addr}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
