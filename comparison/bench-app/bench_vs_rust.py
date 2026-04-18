"""4-way scaling benchmark: fastapi-rs vs Go Gin vs Fastify vs pure Axum.

The pure-axum column shows the absolute Rust ceiling — the framework-overhead
floor that fastapi-rs pays for the Python interop."""
from __future__ import annotations
import socket, time, sys


N = 2000
WARMUP = 100


def _conn(port):
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect(("127.0.0.1", port))
    return s


def _read(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = sock.recv(65536)
        if not c:
            raise RuntimeError("eof")
        buf += c
    hdr, body = buf.split(b"\r\n\r\n", 1)
    cl = 0
    for line in hdr.decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            cl = int(line.split(":", 1)[1].strip())
    while len(body) < cl:
        c = sock.recv(65536)
        if not c:
            raise RuntimeError("short")
        body += c
    return body[:cl]


def bench(port, req):
    s = _conn(port)
    try:
        for _ in range(WARMUP):
            s.sendall(req)
            _read(s)
        lats = []
        t0 = time.perf_counter()
        for _ in range(N):
            t = time.perf_counter()
            s.sendall(req)
            _read(s)
            lats.append((time.perf_counter() - t) * 1e6)
        total = time.perf_counter() - t0
    finally:
        s.close()
    lats.sort()
    return lats[len(lats) // 2], N / total


def upload_req(size, port):
    boundary = "----B"
    payload = b"x" * size
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="f.bin"\r\n'
        f"Content-Type: application/octet-stream\r\n"
        f"\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    head = (
        f"POST /upload HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode()
    return head + body


def download_req(name, port):
    return (
        f"GET /download/{name} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode()


def static_req(name, port):
    return (
        f"GET /static/{name} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode()


def json_req(port, accept_gzip=False):
    h = (
        f"GET /json-big HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n"
    )
    if accept_gzip:
        h += "Accept-Encoding: gzip\r\n"
    h += "\r\n"
    return h.encode()


def main():
    # bench_vs_rust.py rs,go,js,ax
    p_rs, p_go, p_js, p_ax = [int(x) for x in sys.argv[1].split(",")]
    fws = [("fastapi-rs", p_rs), ("Go-Gin", p_go), ("Fastify", p_js), ("pure-axum", p_ax)]

    def run_row(label, req_builder):
        row = [f"{label:<20}"]
        for _, port in fws:
            try:
                p50, rps = bench(port, req_builder(port))
                row.append(f"{int(p50):<10}")
            except Exception as e:
                row.append(f"ERR({e.__class__.__name__})")
        print("".join(row))

    print(f"{'Endpoint':<20}" + "".join(f"{n:<10}" for n, _ in fws))
    print("-" * 60)

    # Uploads at various sizes
    for sz in [1024, 16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024]:
        run_row(f"upload {sz}", lambda port, sz=sz: upload_req(sz, port))

    # Downloads
    for name in ["small.txt", "medium.bin", "large.bin"]:
        run_row(f"download {name}", lambda port, n=name: download_req(n, port))

    # Static file
    run_row("static style.css", lambda port: static_req("style.css", port))

    # JSON plain + gzipped
    run_row("json-big plain", lambda port: json_req(port, False))
    run_row("json-big gzip", lambda port: json_req(port, True))


if __name__ == "__main__":
    main()
