// Database benchmark API -- Pure Rust Axum implementation.
//
// Uses tokio-postgres (NOT sqlx) + bb8 connection pool + redis crate.
// Zero framework overhead baseline for comparison.
//
// Build: cd db_rust_axum && cargo build --release
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use bb8::Pool;
use bb8_postgres::PostgresConnectionManager;
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio_postgres::NoTls;

// ── Models ─────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct ProductOut {
    id: i32,
    name: String,
    price: f64,
    stock: i32,
    category_name: String,
}

#[derive(Deserialize)]
struct ProductCreate {
    name: String,
    #[serde(default)]
    description: String,
    price: f64,
    category_id: i32,
    #[serde(default)]
    stock: i32,
}

#[derive(Serialize)]
struct CategoryStats {
    id: i32,
    name: String,
    product_count: i64,
    avg_price: f64,
    total_stock: i64,
}

#[derive(Serialize)]
struct OrderOut {
    id: i32,
    user_id: i32,
    total: f64,
    status: String,
    created_at: String,
}

#[derive(Serialize)]
struct OrderItemOut {
    id: i32,
    order_id: i32,
    product_id: i32,
    quantity: i32,
    unit_price: f64,
    product_name: String,
}

#[derive(Serialize)]
struct OrderResponse {
    order: OrderOut,
    items: Vec<OrderItemOut>,
}

#[derive(Deserialize)]
struct ListParams {
    #[serde(default = "default_limit")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

fn default_limit() -> i64 {
    10
}

// ── App State ──────────────────────────────────────────────────────────

type PgPool = Pool<PostgresConnectionManager<NoTls>>;

struct AppState {
    db: PgPool,
    redis: redis::aio::MultiplexedConnection,
}

// ── Handlers ───────────────────────────────────────────────────────────

async fn health() -> impl IntoResponse {
    Json(serde_json::json!({"status": "ok"}))
}

async fn get_product(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i32>,
) -> impl IntoResponse {
    let conn = state.db.get().await.unwrap();
    let row = conn
        .query_opt(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name \
             FROM products p JOIN categories c ON p.category_id = c.id \
             WHERE p.id = $1",
            &[&id],
        )
        .await
        .unwrap();

    match row {
        Some(row) => {
            let price: rust_decimal::Decimal = row.get("price");
            let product = ProductOut {
                id: row.get("id"),
                name: row.get("name"),
                price: price.to_string().parse::<f64>().unwrap_or(0.0),
                stock: row.get("stock"),
                category_name: row.get("category_name"),
            };
            (StatusCode::OK, Json(serde_json::to_value(product).unwrap()))
        }
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({"detail": "Product not found"})),
        ),
    }
}

async fn list_products(
    State(state): State<Arc<AppState>>,
    Query(params): Query<ListParams>,
) -> impl IntoResponse {
    let conn = state.db.get().await.unwrap();
    let rows = conn
        .query(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name \
             FROM products p JOIN categories c ON p.category_id = c.id \
             ORDER BY p.id LIMIT $1 OFFSET $2",
            &[&params.limit, &params.offset],
        )
        .await
        .unwrap();

    let products: Vec<ProductOut> = rows
        .iter()
        .map(|row| {
            let price: rust_decimal::Decimal = row.get("price");
            ProductOut {
                id: row.get("id"),
                name: row.get("name"),
                price: price.to_string().parse::<f64>().unwrap_or(0.0),
                stock: row.get("stock"),
                category_name: row.get("category_name"),
            }
        })
        .collect();

    Json(serde_json::to_value(products).unwrap())
}

async fn create_product(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ProductCreate>,
) -> impl IntoResponse {
    let conn = state.db.get().await.unwrap();
    let price_decimal =
        rust_decimal::Decimal::from_str_exact(&format!("{:.2}", body.price)).unwrap();
    let row = conn
        .query_one(
            "INSERT INTO products (name, description, price, category_id, stock) \
             VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock",
            &[
                &body.name,
                &body.description,
                &price_decimal,
                &body.category_id,
                &body.stock,
            ],
        )
        .await
        .unwrap();

    let price: rust_decimal::Decimal = row.get("price");
    let product = ProductOut {
        id: row.get("id"),
        name: row.get("name"),
        price: price.to_string().parse::<f64>().unwrap_or(0.0),
        stock: row.get("stock"),
        category_name: String::new(),
    };

    (
        StatusCode::CREATED,
        Json(serde_json::to_value(product).unwrap()),
    )
}

async fn category_stats(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let conn = state.db.get().await.unwrap();
    let rows = conn
        .query(
            "SELECT c.id, c.name, COUNT(p.id) as product_count, \
             COALESCE(AVG(p.price), 0) as avg_price, \
             COALESCE(SUM(p.stock), 0) as total_stock \
             FROM categories c LEFT JOIN products p ON c.id = p.category_id \
             GROUP BY c.id, c.name ORDER BY c.name",
            &[],
        )
        .await
        .unwrap();

    let stats: Vec<CategoryStats> = rows
        .iter()
        .map(|row| {
            let avg_price: rust_decimal::Decimal = row.get("avg_price");
            let total_stock: i64 = row.get("total_stock");
            CategoryStats {
                id: row.get("id"),
                name: row.get("name"),
                product_count: row.get("product_count"),
                avg_price: avg_price.to_string().parse::<f64>().unwrap_or(0.0),
                total_stock,
            }
        })
        .collect();

    Json(serde_json::to_value(stats).unwrap())
}

async fn get_cached_product(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i32>,
) -> impl IntoResponse {
    let cache_key = format!("product:{}", id);

    // Try Redis cache first (clone is cheap for MultiplexedConnection)
    let mut redis_conn = state.redis.clone();
    if let Ok(cached) = redis_conn.get::<_, String>(&cache_key).await {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&cached) {
            return (StatusCode::OK, Json(val));
        }
    }

    // Cache miss -- query DB
    let conn = state.db.get().await.unwrap();
    let row = conn
        .query_opt(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name \
             FROM products p JOIN categories c ON p.category_id = c.id \
             WHERE p.id = $1",
            &[&id],
        )
        .await
        .unwrap();

    match row {
        Some(row) => {
            let price: rust_decimal::Decimal = row.get("price");
            let product = ProductOut {
                id: row.get("id"),
                name: row.get("name"),
                price: price.to_string().parse::<f64>().unwrap_or(0.0),
                stock: row.get("stock"),
                category_name: row.get("category_name"),
            };
            let json_val = serde_json::to_value(&product).unwrap();

            // Store in Redis with 60s TTL
            let mut redis_conn = state.redis.clone();
            let _: Result<(), _> = redis_conn
                .set_ex(&cache_key, serde_json::to_string(&json_val).unwrap(), 60)
                .await;

            (StatusCode::OK, Json(json_val))
        }
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({"detail": "Product not found"})),
        ),
    }
}

async fn get_order(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i32>,
) -> impl IntoResponse {
    let conn = state.db.get().await.unwrap();

    // Fetch order
    let order_row = conn
        .query_opt(
            "SELECT id, user_id, total, status, created_at FROM orders WHERE id = $1",
            &[&id],
        )
        .await
        .unwrap();

    let order_row = match order_row {
        Some(r) => r,
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({"detail": "Order not found"})),
            );
        }
    };

    let total: rust_decimal::Decimal = order_row.get("total");
    let created_at: chrono::NaiveDateTime = order_row.get("created_at");
    let order = OrderOut {
        id: order_row.get("id"),
        user_id: order_row.get("user_id"),
        total: total.to_string().parse::<f64>().unwrap_or(0.0),
        status: order_row.get("status"),
        created_at: created_at.format("%Y-%m-%dT%H:%M:%S").to_string(),
    };

    // Fetch order items
    let item_rows = conn
        .query(
            "SELECT oi.id, oi.order_id, oi.product_id, oi.quantity, oi.unit_price, \
             p.name as product_name \
             FROM order_items oi \
             JOIN products p ON oi.product_id = p.id \
             WHERE oi.order_id = $1",
            &[&id],
        )
        .await
        .unwrap();

    let items: Vec<OrderItemOut> = item_rows
        .iter()
        .map(|row| {
            let unit_price: rust_decimal::Decimal = row.get("unit_price");
            OrderItemOut {
                id: row.get("id"),
                order_id: row.get("order_id"),
                product_id: row.get("product_id"),
                quantity: row.get("quantity"),
                unit_price: unit_price.to_string().parse::<f64>().unwrap_or(0.0),
                product_name: row.get("product_name"),
            }
        })
        .collect();

    let resp = OrderResponse { order, items };
    (StatusCode::OK, Json(serde_json::to_value(resp).unwrap()))
}

// ── Main ───────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    // PostgreSQL connection pool (min=5, max=20)
    let pg_config = "host=localhost user=venky dbname=fastapi_rs_bench"
        .parse::<tokio_postgres::Config>()
        .unwrap();
    let manager = PostgresConnectionManager::new(pg_config, NoTls);
    let db_pool = Pool::builder()
        .min_idle(Some(5))
        .max_size(20)
        .build(manager)
        .await
        .expect("Failed to create PG pool");

    // Verify PG connection
    {
        let conn = db_pool.get().await.expect("Failed to get PG connection");
        conn.query_one("SELECT 1", &[])
            .await
            .expect("Failed to ping PG");
    }

    // Redis client -- store the multiplexed connection (clone is cheap)
    let redis_client =
        redis::Client::open("redis://127.0.0.1/").expect("Failed to create Redis client");
    let mut redis_conn = redis_client
        .get_multiplexed_async_connection()
        .await
        .expect("Failed to connect to Redis");

    // Verify Redis connection
    let _: String = redis::cmd("PING")
        .query_async(&mut redis_conn)
        .await
        .expect("Failed to ping Redis");

    let state = Arc::new(AppState {
        db: db_pool,
        redis: redis_conn,
    });

    let app = Router::new()
        .route("/health", get(health))
        .route("/products/{id}", get(get_product))
        .route("/products", get(list_products).post(create_product))
        .route("/categories/stats", get(category_stats))
        .route("/cached/products/{id}", get(get_cached_product))
        .route("/orders/{id}", get(get_order))
        .with_state(state);

    let port = std::env::var("PORT").unwrap_or_else(|_| "19032".to_string());
    let addr = format!("127.0.0.1:{}", port);
    eprintln!("Rust Axum DB server listening on {}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
