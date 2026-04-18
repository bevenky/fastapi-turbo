use axum::{
    extract::Query,
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use tokio::net::TcpListener;

#[derive(Serialize)]
struct PingResponse {
    ping: &'static str,
}

#[derive(Serialize)]
struct HelloResponse {
    message: &'static str,
}

#[derive(Serialize)]
struct UserResponse {
    user: &'static str,
}

#[derive(Deserialize)]
struct Item {
    name: String,
    price: f64,
}

#[derive(Serialize)]
struct ItemResponse {
    name: String,
    price: f64,
    created: bool,
}

// Simulated DI functions (inlined for fair comparison with Go)
fn get_db() -> HashMap<&'static str, bool> {
    let mut m = HashMap::new();
    m.insert("connected", true);
    m
}

fn get_user(_db: &HashMap<&str, bool>, _auth: &str) -> &'static str {
    "alice"
}

async fn ping() -> Json<PingResponse> {
    Json(PingResponse { ping: "pong" })
}

async fn hello() -> Json<HelloResponse> {
    Json(HelloResponse { message: "hello" })
}

async fn with_deps(
    headers: axum::http::HeaderMap,
) -> Json<UserResponse> {
    let auth = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("token");
    let db = get_db();
    let user = get_user(&db, auth);
    Json(UserResponse { user })
}

async fn create_item(Json(item): Json<Item>) -> Json<ItemResponse> {
    Json(ItemResponse {
        name: item.name,
        price: item.price,
        created: true,
    })
}

// Form data endpoint
async fn create_form_item(
    axum::extract::Form(item): axum::extract::Form<Item>,
) -> Json<ItemResponse> {
    Json(ItemResponse {
        name: item.name,
        price: item.price,
        created: true,
    })
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "8002".to_string());

    let app = Router::new()
        .route("/_ping", get(ping))
        .route("/hello", get(hello))
        .route("/with-deps", get(with_deps))
        .route("/items", post(create_item))
        .route("/form-items", post(create_form_item));

    let addr = format!("127.0.0.1:{port}");
    println!("Pure Axum running on http://{addr}");
    let listener = TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
