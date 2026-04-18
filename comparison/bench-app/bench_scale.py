"""Scaling benchmark: see how upload/download latency grows with payload size
to tell fixed Python overhead apart from per-byte copy costs."""
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


def bench(label, port, req):
    s = _conn(port)
    try:
        for _ in range(WARMUP):
            s.sendall(req)
            _read(s)
        t0 = time.perf_counter()
        lats = []
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


def main():
    # bench_scale.py rs_port go_port fastify_port
    p_rs, p_go, p_js = [int(x) for x in sys.argv[1].split(",")]
    frameworks = [("rs", p_rs), ("go", p_go), ("js", p_js)]

    sizes_upload = [1024, 16*1024, 64*1024, 256*1024, 1024*1024, 4*1024*1024]
    sizes_names = ["small.txt", "medium.bin", "large.bin"]  # 120B, 64KB, 1MB

    print("Upload scaling (p50 μs):")
    print(f"{'Size':<10}" + "".join(f"{n:<12}" for n, _ in frameworks))
    for sz in sizes_upload:
        row = [f"{sz:<10}"]
        for _, port in frameworks:
            try:
                p50, rps = bench(str(sz), port, upload_req(sz, port))
                row.append(f"{int(p50):<12}")
            except Exception as e:
                row.append(f"ERR({e.__class__.__name__})")
        print("".join(row))

    print("\nDownload scaling (p50 μs):")
    print(f"{'File':<12}" + "".join(f"{n:<12}" for n, _ in frameworks))
    for name in sizes_names:
        row = [f"{name:<12}"]
        for _, port in frameworks:
            try:
                p50, rps = bench(name, port, download_req(name, port))
                row.append(f"{int(p50):<12}")
            except Exception as e:
                row.append(f"ERR({e.__class__.__name__})")
        print("".join(row))


if __name__ == "__main__":
    main()
