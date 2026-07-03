//! ws-bench — WebSocket echo benchmark client (Rust, blocking sockets).
//!
//! Exists because the published WS latency table was measured with a
//! pure-Python client (websockets + uvloop) whose own event-loop wakes
//! contribute ~half of the client-observed RTT. This client is a single
//! blocking thread per connection over a nodelay TcpStream: what it prints
//! is the server round trip plus the kernel, nothing else.
//!
//! Modes:
//!   ws-bench lat ws://HOST:PORT/ws [--n N] [--warmup W] [--size B] [--json]
//!       1 connection, N sequential text echoes of a B-byte message.
//!       Prints ranked-array percentiles in microseconds.
//!
//!   ws-bench tp  ws://HOST:PORT/ws [--conns K] [--pipeline M] [--dur S]
//!                [--size B] [--json]
//!       K connections (one OS thread each), each keeps M echoes in flight
//!       for S seconds. Prints summed msgs/s.
//!
//! Echo contract (bench_ws.py): server echoes the SAME text back verbatim.

use std::net::TcpStream;
use std::time::{Duration, Instant};

use tungstenite::client::client;
use tungstenite::{Message, WebSocket};

struct Args {
    mode: String,
    url: String,
    n: usize,
    warmup: usize,
    conns: usize,
    pipeline: usize,
    dur: f64,
    size: usize,
    json: bool,
}

fn parse_args() -> Args {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut positional: Vec<String> = Vec::new();
    let mut a = Args {
        mode: String::new(),
        url: String::new(),
        n: 20_000,
        warmup: 2_000,
        conns: 8,
        pipeline: 16,
        dur: 5.0,
        size: 64,
        json: false,
    };
    let mut i = 0;
    while i < raw.len() {
        match raw[i].as_str() {
            "--n" => {
                a.n = raw[i + 1].parse().expect("--n");
                i += 2;
            }
            "--warmup" => {
                a.warmup = raw[i + 1].parse().expect("--warmup");
                i += 2;
            }
            "--conns" => {
                a.conns = raw[i + 1].parse().expect("--conns");
                i += 2;
            }
            "--pipeline" => {
                a.pipeline = raw[i + 1].parse().expect("--pipeline");
                i += 2;
            }
            "--dur" => {
                a.dur = raw[i + 1].parse().expect("--dur");
                i += 2;
            }
            "--size" => {
                a.size = raw[i + 1].parse().expect("--size");
                i += 2;
            }
            "--json" => {
                a.json = true;
                i += 1;
            }
            other => {
                positional.push(other.to_string());
                i += 1;
            }
        }
    }
    if positional.len() < 2 {
        eprintln!(
            "usage: ws-bench lat ws://HOST:PORT/ws [--n N] [--warmup W] [--size B] [--json]\n\
             \x20      ws-bench tp  ws://HOST:PORT/ws [--conns K] [--pipeline M] [--dur S] [--size B] [--json]"
        );
        std::process::exit(2);
    }
    a.mode = positional[0].clone();
    a.url = positional[1].clone();
    a
}

/// Connect over a plain TcpStream with Nagle disabled (nodelay is the whole
/// point: a 64-byte echo must not sit in the kernel for 40ms).
fn ws_connect(url: &str) -> WebSocket<TcpStream> {
    let hostport = url
        .strip_prefix("ws://")
        .expect("only ws:// supported")
        .split('/')
        .next()
        .expect("host");
    let stream = TcpStream::connect(hostport).expect("tcp connect");
    stream.set_nodelay(true).expect("nodelay");
    let (socket, _resp) = client(url, stream).expect("ws handshake");
    socket
}

/// Read frames until a Text/Binary data frame arrives (transparently answer
/// pings the way every real client does).
fn recv_data(ws: &mut WebSocket<TcpStream>) -> Message {
    loop {
        match ws.read().expect("ws read") {
            m @ (Message::Text(_) | Message::Binary(_)) => return m,
            Message::Ping(p) => {
                let _ = ws.send(Message::Pong(p));
            }
            Message::Close(c) => panic!("server closed mid-bench: {c:?}"),
            _ => continue,
        }
    }
}

fn pct(sorted: &[f64], p: f64) -> f64 {
    // Ranked-array convention, identical to bench_ws.py:
    // samples[int(p * (len-1))]
    sorted[(p * (sorted.len() - 1) as f64) as usize]
}

fn run_latency(a: &Args) {
    let msg = "x".repeat(a.size);
    let mut ws = ws_connect(&a.url);

    // Contract smoke check first — a broken echo must not be benchmarked.
    ws.send(Message::text(msg.clone())).expect("send");
    match recv_data(&mut ws) {
        Message::Text(t) if t.as_str() == msg => {}
        other => panic!("echo contract violated: {other:?}"),
    }

    let total = a.warmup + a.n;
    let mut samples: Vec<f64> = Vec::with_capacity(a.n);
    for i in 0..total {
        let t0 = Instant::now();
        ws.send(Message::text(msg.clone())).expect("send");
        let echo = recv_data(&mut ws);
        let dt = t0.elapsed().as_nanos() as f64 / 1000.0;
        match echo {
            Message::Text(t) if t.as_str() == msg => {}
            other => panic!("echo mismatch at {i}: {other:?}"),
        }
        if i >= a.warmup {
            samples.push(dt);
        }
    }
    let _ = ws.close(None);

    samples.sort_by(|x, y| x.partial_cmp(y).unwrap());
    let (min, max) = (samples[0], *samples.last().unwrap());
    let (p50, p90, p99, p999) = (
        pct(&samples, 0.50),
        pct(&samples, 0.90),
        pct(&samples, 0.99),
        pct(&samples, 0.999),
    );
    if a.json {
        println!(
            "{{\"mode\":\"lat\",\"n\":{},\"size\":{},\"min_us\":{:.1},\"p50_us\":{:.1},\"p90_us\":{:.1},\"p99_us\":{:.1},\"p999_us\":{:.1},\"max_us\":{:.1}}}",
            samples.len(),
            a.size,
            min,
            p50,
            p90,
            p99,
            p999,
            max
        );
    } else {
        println!(
            "lat n={} size={}B | p50={:.1}us p90={:.1}us p99={:.1}us p999={:.1}us min={:.1}us max={:.1}us",
            samples.len(),
            a.size,
            p50,
            p90,
            p99,
            p999,
            min,
            max
        );
    }
}

fn run_throughput(a: &Args) {
    let deadline = Duration::from_secs_f64(a.dur);
    let handles: Vec<_> = (0..a.conns)
        .map(|_| {
            let url = a.url.clone();
            let msg = "x".repeat(a.size);
            let pipeline = a.pipeline;
            std::thread::spawn(move || {
                let mut ws = ws_connect(&url);
                for _ in 0..pipeline {
                    ws.send(Message::text(msg.clone())).expect("prime send");
                }
                let t0 = Instant::now();
                let end = t0 + deadline;
                let mut count: u64 = 0;
                while Instant::now() < end {
                    recv_data(&mut ws);
                    count += 1;
                    ws.send(Message::text(msg.clone())).expect("send");
                }
                for _ in 0..pipeline {
                    recv_data(&mut ws);
                    count += 1;
                }
                let elapsed = t0.elapsed().as_secs_f64();
                let _ = ws.close(None);
                count as f64 / elapsed
            })
        })
        .collect();
    let total: f64 = handles.into_iter().map(|h| h.join().expect("worker")).sum();
    if a.json {
        println!(
            "{{\"mode\":\"tp\",\"conns\":{},\"pipeline\":{},\"dur_s\":{},\"size\":{},\"msgs_per_s\":{:.0}}}",
            a.conns, a.pipeline, a.dur, a.size, total
        );
    } else {
        println!(
            "tp conns={} pipeline={} dur={}s size={}B | {:.0} msgs/s",
            a.conns, a.pipeline, a.dur, a.size, total
        );
    }
}

fn main() {
    let a = parse_args();
    match a.mode.as_str() {
        "lat" => run_latency(&a),
        "tp" => run_throughput(&a),
        m => {
            eprintln!("unknown mode {m:?} (want: lat | tp)");
            std::process::exit(2);
        }
    }
}
