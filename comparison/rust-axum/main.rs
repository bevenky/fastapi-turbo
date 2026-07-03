use axum::{
    response::Json,
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

// Matches the bench contract used by _bench_app.py / go-gin / fastify:
// {"sku": str, "qty": int, "tags": [str] = []}
#[derive(Deserialize)]
struct Item {
    sku: String,
    qty: i64,
    #[serde(default)]
    tags: Vec<String>,
}

#[derive(Serialize)]
struct ItemResponse {
    ok: bool,
    sku: String,
    qty: i64,
    tag_count: usize,
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

#[derive(Serialize)]
struct ListItem {
    id: usize,
    name: String,
}

#[derive(Serialize)]
struct ListResponse {
    items: Vec<ListItem>,
}

// Mirrors benchmarks/_bench_app.py `/list` — 20-item JSON payload.
async fn list_items() -> Json<ListResponse> {
    let items = (0..20)
        .map(|i| ListItem {
            id: i,
            name: format!("item-{i}"),
        })
        .collect();
    Json(ListResponse { items })
}

async fn create_item(Json(item): Json<Item>) -> Json<ItemResponse> {
    Json(ItemResponse {
        ok: true,
        tag_count: item.tags.len(),
        sku: item.sku,
        qty: item.qty,
    })
}

// Form data endpoint (sku/qty only — serde_urlencoded can't do Vec)
#[derive(Deserialize)]
struct FormItem {
    sku: String,
    qty: i64,
}

async fn create_form_item(
    axum::extract::Form(item): axum::extract::Form<FormItem>,
) -> Json<ItemResponse> {
    Json(ItemResponse {
        ok: true,
        sku: item.sku,
        qty: item.qty,
        tag_count: 0,
    })
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "8002".to_string());

    let app = Router::new()
        .route("/_ping", get(ping))
        .route("/hello", get(hello))
        .route("/with-deps", get(with_deps))
        .route("/list", get(list_items))
        .route("/items", post(create_item))
        .route("/form-items", post(create_form_item));

    let addr = format!("127.0.0.1:{port}");
    println!("Pure Axum running on http://{addr}");
    let listener = TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
