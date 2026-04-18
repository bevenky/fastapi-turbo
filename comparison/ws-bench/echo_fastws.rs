/// fastwebsockets echo server using axum + IncomingUpgrade.
use axum::{extract::Request, response::Response, routing::any, Router};
use fastwebsockets::upgrade::IncomingUpgrade;
use fastwebsockets::{Frame, OpCode};
use tokio::net::TcpListener;

async fn ws_handler(req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let req = hyper::Request::from_parts(parts, body);
    let (mut parts, body) = req.into_parts();

    // Manually extract IncomingUpgrade
    let key = parts.headers.get("sec-websocket-key").cloned();
    if key.is_none() {
        return Response::builder().status(400).body(axum::body::Body::empty()).unwrap();
    }

    let mut req = hyper::Request::from_parts(parts, body);
    let (response, fut) = fastwebsockets::upgrade::upgrade(&mut req).unwrap();

    tokio::spawn(async move {
        let mut ws = fut.await.unwrap();
        ws.set_auto_close(true);
        ws.set_auto_pong(true);
        loop {
            let frame = match ws.read_frame().await {
                Ok(f) => f,
                Err(_) => break,
            };
            match frame.opcode {
                OpCode::Close => break,
                OpCode::Text | OpCode::Binary => {
                    let _ = ws.write_frame(Frame::new(true, frame.opcode, None, frame.payload)).await;
                }
                _ => {}
            }
        }
    });

    let (parts, body) = response.into_parts();
    Response::from_parts(parts, axum::body::Body::new(body))
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "9002".to_string());
    let addr = format!("127.0.0.1:{port}");
    println!("fastwebsockets echo on ws://{addr}/ws");
    let app = Router::new().route("/ws", any(ws_handler));
    let listener = TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
