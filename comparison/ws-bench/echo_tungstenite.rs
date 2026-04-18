/// Pure tungstenite echo server (via axum::extract::ws) — baseline measurement.
use axum::{extract::ws::{Message, WebSocket, WebSocketUpgrade}, routing::any, Router};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;

async fn ws_handler(ws: WebSocketUpgrade) -> axum::response::Response {
    ws.on_upgrade(handle_socket)
}

async fn handle_socket(mut socket: WebSocket) {
    while let Some(Ok(msg)) = socket.next().await {
        match msg {
            Message::Text(_) | Message::Binary(_) => {
                if socket.send(msg).await.is_err() { break; }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "9001".to_string());
    let app = Router::new().route("/ws", any(ws_handler));
    let addr = format!("127.0.0.1:{port}");
    println!("tungstenite echo on ws://{addr}/ws");
    let listener = TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
