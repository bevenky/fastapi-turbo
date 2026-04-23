//! Rust HTTP benchmark client for framework comparison.
//!
//! Usage: fastapi-turbo-bench HOST PORT PATH [N] [WARMUP] [METHOD] [BODY] [CONTENT_TYPE]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let host = args.get(1).map(|s| s.as_str()).unwrap_or("127.0.0.1");
    let port: u16 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(8000);
    let path = args.get(3).map(|s| s.as_str()).unwrap_or("/hello");
    let n: usize = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(10000);
    let warmup: usize = args.get(5).and_then(|s| s.parse().ok()).unwrap_or(1000);
    let method = args.get(6).map(|s| s.as_str()).unwrap_or("GET");
    let body = args.get(7).map(|s| s.as_str()).unwrap_or("");
    let content_type = args.get(8).map(|s| s.as_str()).unwrap_or("application/json");

    let addr = format!("{host}:{port}");

    let request_bytes = if method == "GET" {
        format!(
            "GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n"
        )
    } else if body.is_empty() {
        // Methods like DELETE frequently carry no body. Don't force a
        // Content-Type header (some servers, e.g. Fastify, 400 on an
        // empty JSON body) and send Content-Length: 0.
        format!(
            "{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\nContent-Length: 0\r\n\r\n"
        )
    } else {
        format!(
            "{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
    };

    let mut stream = TcpStream::connect(&addr).expect("Failed to connect");
    stream.set_nodelay(true).ok();

    // Warmup
    for _ in 0..warmup {
        send_recv(&mut stream, request_bytes.as_bytes());
    }

    let mut client_latencies: Vec<f64> = Vec::with_capacity(n);
    let mut server_latencies: Vec<f64> = Vec::with_capacity(n);

    let total_start = Instant::now();
    for _ in 0..n {
        let start = Instant::now();
        let response = send_recv(&mut stream, request_bytes.as_bytes());
        let client_us = start.elapsed().as_nanos() as f64 / 1000.0;
        client_latencies.push(client_us);

        if let Some(st) = parse_server_timing(&response) {
            server_latencies.push(st);
        }
    }
    let total_elapsed = total_start.elapsed();

    client_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    // Compact output for table generation
    let p50_c = client_latencies[client_latencies.len() / 2];
    let p99_c = client_latencies[(client_latencies.len() as f64 * 0.99) as usize];
    let min_c = client_latencies[0];
    let rps = n as f64 / total_elapsed.as_secs_f64();

    if !server_latencies.is_empty() {
        server_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let p50_s = server_latencies[server_latencies.len() / 2];
        println!(
            "  client p50={p50_c:.0}μs p99={p99_c:.0}μs min={min_c:.0}μs | server p50={p50_s:.0}μs | {rps:.0} req/s"
        );
    } else {
        println!("  client p50={p50_c:.0}μs p99={p99_c:.0}μs min={min_c:.0}μs | {rps:.0} req/s");
    }
}

fn send_recv(stream: &mut TcpStream, request: &[u8]) -> String {
    stream.write_all(request).expect("write failed");
    let mut buf = [0u8; 16384];
    let mut response = Vec::new();
    loop {
        let n = stream.read(&mut buf).expect("read failed");
        if n == 0 { break; }
        response.extend_from_slice(&buf[..n]);
        if let Some(header_end) = find_subsequence(&response, b"\r\n\r\n") {
            let headers = std::str::from_utf8(&response[..header_end]).unwrap_or("");
            let cl = headers.lines()
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
    String::from_utf8_lossy(&response).to_string()
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn parse_server_timing(response: &str) -> Option<f64> {
    for line in response.lines() {
        if line.to_lowercase().starts_with("server-timing:") {
            if let Some(dur_part) = line.split("dur=").nth(1) {
                let dur_str = dur_part.split(|c: char| !c.is_ascii_digit()).next()?;
                return dur_str.parse::<f64>().ok();
            }
        }
    }
    None
}
