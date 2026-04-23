// Mini e-commerce bench app — pure Rust Axum baseline (no DB).
use axum::{
    extract::{ws::{Message, WebSocket, WebSocketUpgrade}, Path, Query, State},
    http::{header::AUTHORIZATION, StatusCode},
    response::IntoResponse,
    routing::{delete, get, patch, post, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, sync::{Arc, Mutex}};

#[derive(Clone, Serialize, Deserialize)]
struct Item {
    id: i32,
    name: String,
    price: f64,
    description: Option<String>,
}

#[derive(Deserialize)]
struct ItemIn {
    name: String,
    price: f64,
    description: Option<String>,
}

#[derive(Deserialize)]
struct Pagination {
    limit: Option<usize>,
    offset: Option<usize>,
}

#[derive(Clone)]
struct AppState {
    db: Arc<Mutex<(i32, HashMap<i32, Item>)>>,
}

const SECRET: &str = "secret-token-123";

async fn health() -> impl IntoResponse {
    Json(serde_json::json!({"status": "ok"}))
}

async fn list_items(State(s): State<AppState>, Query(p): Query<Pagination>) -> impl IntoResponse {
    let g = s.db.lock().unwrap();
    let mut items: Vec<Item> = g.1.values().cloned().collect();
    items.sort_by_key(|i| i.id);
    let offset = p.offset.unwrap_or(0);
    let limit = p.limit.unwrap_or(10);
    let end = (offset + limit).min(items.len());
    let slice = if offset >= items.len() { vec![] } else { items[offset..end].to_vec() };
    Json(serde_json::to_value(slice).unwrap())
}

async fn get_item(State(s): State<AppState>, Path(id): Path<i32>) -> impl IntoResponse {
    let g = s.db.lock().unwrap();
    match g.1.get(&id) {
        Some(i) => Json(serde_json::to_value(i).unwrap()).into_response(),
        None => (StatusCode::NOT_FOUND, Json(serde_json::json!({"detail": "Item not found"}))).into_response(),
    }
}

async fn create_item(State(s): State<AppState>, Json(input): Json<ItemIn>) -> impl IntoResponse {
    let mut g = s.db.lock().unwrap();
    g.0 += 1;
    let id = g.0;
    let item = Item { id, name: input.name, price: input.price, description: input.description };
    g.1.insert(id, item.clone());
    (StatusCode::CREATED, Json(serde_json::to_value(item).unwrap()))
}

async fn update_item(State(s): State<AppState>, Path(id): Path<i32>, Json(input): Json<ItemIn>) -> impl IntoResponse {
    let mut g = s.db.lock().unwrap();
    if !g.1.contains_key(&id) {
        return (StatusCode::NOT_FOUND, Json(serde_json::json!({"detail": "Item not found"}))).into_response();
    }
    let item = Item { id, name: input.name, price: input.price, description: input.description };
    g.1.insert(id, item.clone());
    Json(serde_json::to_value(item).unwrap()).into_response()
}

async fn delete_item(State(s): State<AppState>, Path(id): Path<i32>) -> impl IntoResponse {
    let mut g = s.db.lock().unwrap();
    g.1.remove(&id);
    StatusCode::NO_CONTENT
}

async fn users_me(headers: axum::http::HeaderMap) -> impl IntoResponse {
    let auth = headers.get(AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("");
    if !auth.starts_with("Bearer ") || &auth[7..] != SECRET {
        return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"detail": "Missing or invalid token"}))).into_response();
    }
    Json(serde_json::json!({"username": "demo_user", "email": "demo@example.com"})).into_response()
}

async fn ws_chat(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(handle_ws)
}

async fn handle_ws(mut socket: WebSocket) {
    while let Some(Ok(msg)) = socket.recv().await {
        match msg {
            Message::Text(t) => {
                // Parse JSON, add server_ts, send back.
                let mut v: serde_json::Value = match serde_json::from_str(&t) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                let ts = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs_f64();
                v["server_ts"] = serde_json::json!(ts);
                let out = serde_json::to_string(&v).unwrap();
                if socket.send(Message::Text(out.into())).await.is_err() {
                    break;
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

#[tokio::main]
async fn main() {
    let mut db = HashMap::new();
    db.insert(1, Item { id: 1, name: "Widget".into(), price: 9.99, description: None });
    db.insert(2, Item { id: 2, name: "Gadget".into(), price: 19.99, description: None });
    db.insert(3, Item { id: 3, name: "Doohickey".into(), price: 29.99, description: None });
    let state = AppState { db: Arc::new(Mutex::new((3, db))) };

    let app = Router::new()
        .route("/health", get(health))
        .route("/items", get(list_items).post(create_item))
        .route("/items/{id}", get(get_item).put(update_item).patch(update_item).delete(delete_item))
        .route("/users/me", get(users_me))
        .route("/ws/chat", get(ws_chat))
        .with_state(state);

    let port = std::env::var("PORT").unwrap_or_else(|_| "19006".to_string());
    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    eprintln!("Axum ecommerce on {addr}");
    axum::serve(listener, app).await.unwrap();
}
