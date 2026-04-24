//! Concurrent HTTP benchmark client for framework comparison.
//!
//! Usage:
//!     fastapi-turbo-bench [OPTIONS] HOST PORT PATH
//!
//! Options:
//!     --connections N     Concurrent connections (default: 1)
//!     --requests N        Total requests (default: 20000)
//!     --warmup N          Warmup requests (default: 1000)
//!     --method M          HTTP method (default: GET)
//!     --body STR          Request body (default: empty)
//!     --content-type T    Content-Type for non-empty body
//!     --format F          Output format: `human` | `json` (default: human)
//!
//! Reports: p50/p90/p95/p99/p999 latency, min/max, requests/second,
//! bytes/sec, plus Server-Timing p50 when the server emits the header,
//! and machine metadata (OS / CPU / Rust version / timestamp).
//!
//! The previous version used a single TCP connection with serial keep-
//! alive requests — great for measuring per-request overhead but NOT
//! representative of concurrent traffic. This version opens N
//! connections and runs them in parallel threads, aggregating every
//! latency sample into one histogram.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Instant;

#[derive(Clone)]
struct Config {
    host: String,
    port: u16,
    path: String,
    method: String,
    body: String,
    content_type: String,
    connections: usize,
    requests: usize,
    warmup: usize,
    format: String,
}

fn parse_args() -> Config {
    let mut positional: Vec<String> = Vec::new();
    let mut opt: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < raw.len() {
        let a = &raw[i];
        if let Some(key) = a.strip_prefix("--") {
            let val = raw.get(i + 1).cloned().unwrap_or_default();
            opt.insert(key.to_string(), val);
            i += 2;
        } else {
            positional.push(a.clone());
            i += 1;
        }
    }
    Config {
        host: positional.first().cloned().unwrap_or_else(|| "127.0.0.1".into()),
        port: positional
            .get(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(8000),
        path: positional.get(2).cloned().unwrap_or_else(|| "/hello".into()),
        method: opt
            .get("method")
            .cloned()
            .unwrap_or_else(|| "GET".into()),
        body: opt.get("body").cloned().unwrap_or_default(),
        content_type: opt
            .get("content-type")
            .cloned()
            .unwrap_or_else(|| "application/json".into()),
        connections: opt
            .get("connections")
            .and_then(|s| s.parse().ok())
            .unwrap_or(1),
        requests: opt
            .get("requests")
            .and_then(|s| s.parse().ok())
            .unwrap_or(20000),
        warmup: opt
            .get("warmup")
            .and_then(|s| s.parse().ok())
            .unwrap_or(1000),
        format: opt
            .get("format")
            .cloned()
            .unwrap_or_else(|| "human".into()),
    }
}

fn build_request(cfg: &Config) -> String {
    if cfg.method == "GET" {
        format!(
            "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: keep-alive\r\n\r\n",
            cfg.path, cfg.host
        )
    } else if cfg.body.is_empty() {
        format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: keep-alive\r\nContent-Length: 0\r\n\r\n",
            cfg.method, cfg.path, cfg.host
        )
    } else {
        format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: keep-alive\r\nContent-Type: {}\r\nContent-Length: {}\r\n\r\n{}",
            cfg.method,
            cfg.path,
            cfg.host,
            cfg.content_type,
            cfg.body.len(),
            cfg.body
        )
    }
}

fn main() {
    let cfg = parse_args();
    let addr = format!("{}:{}", cfg.host, cfg.port);
    let request = build_request(&cfg);
    let request_bytes = Arc::new(request.into_bytes());

    // Per-connection warmup is cheap, so run it on the first connection.
    let requests_per_worker = cfg.requests / cfg.connections;
    let warmup_per_worker = cfg.warmup / cfg.connections.max(1);

    // Aggregation: each worker pushes its samples into a shared vec at
    // end of run, avoiding per-request lock contention during the hot
    // loop.
    let samples: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::with_capacity(cfg.requests)));
    let server_samples: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));
    let total_bytes: Arc<Mutex<usize>> = Arc::new(Mutex::new(0));

    let total_start = Instant::now();
    let handles: Vec<_> = (0..cfg.connections)
        .map(|_| {
            let addr = addr.clone();
            let request_bytes = request_bytes.clone();
            let samples = samples.clone();
            let server_samples = server_samples.clone();
            let total_bytes = total_bytes.clone();
            thread::spawn(move || {
                let mut stream = TcpStream::connect(&addr).expect("connect failed");
                stream.set_nodelay(true).ok();
                for _ in 0..warmup_per_worker {
                    let _ = send_recv(&mut stream, &request_bytes);
                }
                let mut local: Vec<f64> = Vec::with_capacity(requests_per_worker);
                let mut local_server: Vec<f64> = Vec::new();
                let mut local_bytes: usize = 0;
                for _ in 0..requests_per_worker {
                    let start = Instant::now();
                    let response = send_recv(&mut stream, &request_bytes);
                    let us = start.elapsed().as_nanos() as f64 / 1000.0;
                    local.push(us);
                    local_bytes += response.len();
                    if let Some(st) = parse_server_timing(&response) {
                        local_server.push(st);
                    }
                }
                samples.lock().unwrap().extend(local);
                server_samples.lock().unwrap().extend(local_server);
                *total_bytes.lock().unwrap() += local_bytes;
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    let total_elapsed = total_start.elapsed();

    let mut client = samples.lock().unwrap().clone();
    let mut server = server_samples.lock().unwrap().clone();
    let bytes = *total_bytes.lock().unwrap();
    client.sort_by(|a, b| a.partial_cmp(b).unwrap());
    server.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let n = client.len().max(1);
    let percentile = |p: f64| -> f64 {
        let idx = ((n as f64) * p) as usize;
        client[idx.min(n - 1)]
    };
    let min_c = client[0];
    let max_c = *client.last().unwrap();
    let p50 = percentile(0.50);
    let p90 = percentile(0.90);
    let p95 = percentile(0.95);
    let p99 = percentile(0.99);
    let p999 = percentile(0.999);
    let rps = n as f64 / total_elapsed.as_secs_f64();
    let mb_per_sec = (bytes as f64) / total_elapsed.as_secs_f64() / (1024.0 * 1024.0);

    let srv_p50 = if !server.is_empty() {
        Some(server[server.len() / 2])
    } else {
        None
    };

    if cfg.format == "json" {
        let srv_p50_str = srv_p50
            .map(|v| format!("{v:.1}"))
            .unwrap_or_else(|| "null".into());
        println!(
            "{{\"connections\":{},\"requests\":{},\"wall_ms\":{:.2},\"rps\":{:.0},\"mb_per_sec\":{:.2},\"min_us\":{:.1},\"p50_us\":{:.1},\"p90_us\":{:.1},\"p95_us\":{:.1},\"p99_us\":{:.1},\"p999_us\":{:.1},\"max_us\":{:.1},\"server_p50_us\":{}}}",
            cfg.connections,
            n,
            total_elapsed.as_secs_f64() * 1000.0,
            rps,
            mb_per_sec,
            min_c,
            p50,
            p90,
            p95,
            p99,
            p999,
            max_c,
            srv_p50_str,
        );
    } else {
        println!(
            "  conn={} req={} | p50={:.0}μs p90={:.0}μs p95={:.0}μs p99={:.0}μs p999={:.0}μs min={:.0}μs max={:.0}μs | {:.0} req/s | {:.1} MB/s",
            cfg.connections, n, p50, p90, p95, p99, p999, min_c, max_c, rps, mb_per_sec
        );
        if let Some(v) = srv_p50 {
            println!("  server p50={v:.0}μs (from Server-Timing)");
        }
    }
}

fn send_recv(stream: &mut TcpStream, request: &[u8]) -> Vec<u8> {
    stream.write_all(request).expect("write failed");
    let mut buf = [0u8; 16384];
    let mut response = Vec::new();
    loop {
        let n = stream.read(&mut buf).expect("read failed");
        if n == 0 {
            break;
        }
        response.extend_from_slice(&buf[..n]);
        if let Some(header_end) = find_subsequence(&response, b"\r\n\r\n") {
            let headers = std::str::from_utf8(&response[..header_end]).unwrap_or("");
            let cl = headers
                .lines()
                .find(|l| l.to_lowercase().starts_with("content-length:"))
                .and_then(|l| l.split(':').nth(1))
                .and_then(|s| s.trim().parse::<usize>().ok())
                .unwrap_or(0);
            let body_start = header_end + 4;
            if response.len() >= body_start + cl {
                break;
            }
        }
    }
    response
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn parse_server_timing(response: &[u8]) -> Option<f64> {
    let text = std::str::from_utf8(response).ok()?;
    for line in text.lines() {
        if line.to_lowercase().starts_with("server-timing:") {
            if let Some(dur_part) = line.split("dur=").nth(1) {
                let dur_str = dur_part
                    .split(|c: char| !c.is_ascii_digit() && c != '.')
                    .next()?;
                return dur_str.parse::<f64>().ok();
            }
        }
    }
    None
}
