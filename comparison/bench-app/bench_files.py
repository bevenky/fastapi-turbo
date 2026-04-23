"""File handling benchmarks — fastapi-turbo vs Go Gin vs Fastify.

Measures:
  - POST /upload (multipart small ~1 KB file)
  - POST /upload (multipart medium 64 KB file)
  - GET  /download/small.txt
  - GET  /download/medium.bin  (64 KB)
  - GET  /download/large.bin   (1 MB)
  - GET  /static/style.css     (served by framework's static file middleware)

Uses a single persistent keep-alive TCP connection per test. The server lives
entirely in-memory — no disk I/O except the download/static endpoints.
"""

from __future__ import annotations

import os
import socket
import sys
import time


N = int(os.environ.get("BENCH_N", "5000"))
WARMUP = int(os.environ.get("BENCH_WARMUP", "200"))


def _percentile(sorted_vals: list[float], p: float) -> float:
    idx = int(len(sorted_vals) * p)
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return sorted_vals[idx]


def _open_conn(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((host, port))
    return sock


def _read_response(sock: socket.socket) -> tuple[int, bytes]:
    """Read one HTTP response via Content-Length. Returns (status, body)."""
    buf = b""
    header_end = -1
    while header_end < 0:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("connection closed")
        buf += chunk
        header_end = buf.find(b"\r\n\r\n")

    headers = buf[:header_end].decode("utf-8", errors="replace")
    # Parse status line
    status_line = headers.split("\r\n", 1)[0]
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 0

    # Parse Content-Length or Transfer-Encoding
    content_length = -1
    transfer_encoding = ""
    for line in headers.split("\r\n")[1:]:
        lo = line.lower()
        if lo.startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
        elif lo.startswith("transfer-encoding:"):
            transfer_encoding = line.split(":", 1)[1].strip().lower()

    body_start = header_end + 4
    body = buf[body_start:]

    if content_length >= 0:
        while len(body) < content_length:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("short body")
            body += chunk
        body = body[:content_length]
    elif transfer_encoding == "chunked":
        # Parse chunked encoding
        full = body
        chunks = []
        while True:
            while b"\r\n" not in full:
                c = sock.recv(65536)
                if not c:
                    raise RuntimeError("chunked EOF")
                full += c
            size_line, rest = full.split(b"\r\n", 1)
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                # consume trailing CRLF
                while len(rest) < 2:
                    rest += sock.recv(64)
                break
            while len(rest) < size + 2:
                c = sock.recv(65536)
                if not c:
                    raise RuntimeError("chunk body EOF")
                rest += c
            chunks.append(rest[:size])
            full = rest[size + 2:]
        body = b"".join(chunks)
    else:
        # No length given — read until close (shouldn't happen with keep-alive)
        while True:
            c = sock.recv(65536)
            if not c:
                break
            body += c

    return status, body


def bench(label: str, host: str, port: int, request_bytes: bytes, expected_status: int = 200) -> dict:
    """Run N timed requests over one keep-alive connection."""
    sock = _open_conn(host, port)
    last_body_len = 0
    try:
        # Warmup
        for _ in range(WARMUP):
            sock.sendall(request_bytes)
            status, body = _read_response(sock)
            last_body_len = len(body)
            if status != expected_status:
                raise RuntimeError(f"{label}: got status {status}, expected {expected_status}")

        lats: list[float] = []
        t0 = time.perf_counter()
        for _ in range(N):
            t = time.perf_counter()
            sock.sendall(request_bytes)
            status, _ = _read_response(sock)
            lats.append((time.perf_counter() - t) * 1e6)
            if status != expected_status:
                raise RuntimeError(f"{label}: status {status}")
        total = time.perf_counter() - t0
    finally:
        sock.close()

    lats.sort()
    return {
        "label": label,
        "p50": _percentile(lats, 0.5),
        "p99": _percentile(lats, 0.99),
        "min": lats[0],
        "rps": N / total,
        "body_len": last_body_len,
    }


def build_get(host: str, port: int, path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    ).encode()


def build_multipart_upload(host: str, port: int, path: str, filename: str, payload: bytes, content_type: str = "application/octet-stream") -> bytes:
    boundary = "----FormBoundarybench"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n"
        f"\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()

    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Connection: keep-alive\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode() + body
    return request


def build_gzip_get(host: str, port: int, path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Connection: keep-alive\r\n"
        f"Accept-Encoding: gzip\r\n"
        f"\r\n"
    ).encode()


def run_framework(name: str, host: str, port: int, results: dict):
    print(f"\n=== {name} (port {port}) ===")
    # Upload small (1 KB)
    payload_small = b"x" * 1024
    req = build_multipart_upload(host, port, "/upload", "small.bin", payload_small)
    r = bench("upload-1KB", host, port, req)
    results.setdefault(name, {})["upload-1KB"] = r
    print(f"  upload-1KB      p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # Upload medium (64 KB)
    payload_medium = b"x" * (64 * 1024)
    req = build_multipart_upload(host, port, "/upload", "medium.bin", payload_medium)
    r = bench("upload-64KB", host, port, req)
    results[name]["upload-64KB"] = r
    print(f"  upload-64KB     p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # Download small
    req = build_get(host, port, "/download/small.txt")
    r = bench("dl-small", host, port, req)
    results[name]["dl-small"] = r
    print(f"  dl-small.txt    p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # Download medium
    req = build_get(host, port, "/download/medium.bin")
    r = bench("dl-medium", host, port, req)
    results[name]["dl-medium"] = r
    print(f"  dl-medium.bin   p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # Download large
    req = build_get(host, port, "/download/large.bin")
    r = bench("dl-large", host, port, req)
    results[name]["dl-large"] = r
    print(f"  dl-large.bin    p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # Static CSS
    req = build_get(host, port, "/static/style.css")
    r = bench("static-css", host, port, req)
    results[name]["static-css"] = r
    print(f"  static/style.css p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s")

    # JSON big without gzip
    req = build_get(host, port, "/json-big")
    r = bench("json-big", host, port, req)
    results[name]["json-big"] = r
    print(f"  json-big (plain) p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s  body={r['body_len']}B")

    # JSON big with gzip
    req = build_gzip_get(host, port, "/json-big")
    r = bench("json-big-gz", host, port, req)
    results[name]["json-big-gz"] = r
    print(f"  json-big (gzip)  p50={r['p50']:6.0f}us p99={r['p99']:6.0f}us  {r['rps']:8.0f} req/s  body={r['body_len']}B")


def main():
    if len(sys.argv) < 2:
        print("usage: bench_files.py <port_rs>,<port_go>,<port_fastify>")
        sys.exit(1)

    ports = [int(p) for p in sys.argv[1].split(",")]
    frameworks = ["fastapi-turbo", "Go-Gin", "Fastify"][: len(ports)]

    results: dict = {}
    for name, port in zip(frameworks, ports):
        try:
            run_framework(name, "127.0.0.1", port, results)
        except Exception as e:
            print(f"  ERROR {name}: {e}")

    # Print comparison table
    if not results:
        return
    tests = ["upload-1KB", "upload-64KB", "dl-small", "dl-medium", "dl-large", "static-css", "json-big", "json-big-gz"]
    print("\n\n### Throughput (req/s) — higher is better\n")
    print(f"| {'Endpoint':<18} | " + " | ".join(f"{n:<12}" for n in frameworks) + " |")
    print(f"| {'-'*18} | " + " | ".join("-" * 12 for _ in frameworks) + " |")
    for t in tests:
        row = [f"{int(results.get(n, {}).get(t, {}).get('rps', 0)):<12}" for n in frameworks]
        print(f"| {t:<18} | " + " | ".join(row) + " |")

    print("\n### Latency p50 (us) — lower is better\n")
    print(f"| {'Endpoint':<18} | " + " | ".join(f"{n:<12}" for n in frameworks) + " |")
    print(f"| {'-'*18} | " + " | ".join("-" * 12 for _ in frameworks) + " |")
    for t in tests:
        row = [f"{int(results.get(n, {}).get(t, {}).get('p50', 0)):<12}" for n in frameworks]
        print(f"| {t:<18} | " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
