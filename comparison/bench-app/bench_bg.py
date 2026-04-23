"""Background task benchmark.

For each framework we measure:
  1. Request+task scheduling LATENCY (how fast the handler returns)
  2. Total THROUGHPUT (requests/sec over 5k requests)
  3. Task completion — confirm all N tasks actually ran after the run

Fastapi-rs runs sync BG tasks synchronously before response flush (correct
for small work). Go launches goroutines. Fastify uses setImmediate.
"""
import socket
import sys
import time


N = 5000
WARMUP = 200


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


def bench(label, port, path):
    req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n"
        f"Content-Length: 0\r\n\r\n"
    ).encode()
    s = _conn(port)
    try:
        for _ in range(WARMUP):
            s.sendall(req); _read(s)
        lats = []
        t0 = time.perf_counter()
        for _ in range(N):
            t = time.perf_counter()
            s.sendall(req); _read(s)
            lats.append((time.perf_counter() - t) * 1e6)
        total = time.perf_counter() - t0
    finally:
        s.close()
    lats.sort()
    return {
        "p50": int(lats[len(lats) // 2]),
        "p99": int(lats[int(len(lats) * 0.99)]),
        "rps": int(N / total),
    }


def get_count(port):
    import httpx
    r = httpx.get(f"http://127.0.0.1:{port}/bg/count")
    return r.json().get("count", 0)


def reset(port):
    # no reset endpoint — we just track delta
    return get_count(port)


def main():
    ports = [int(x) for x in sys.argv[1].split(",")]
    names = ["fastapi-turbo", "Go-Gin", "Fastify"][: len(ports)]
    fws = list(zip(names, ports))

    print(f"{'Endpoint':<18}" + "".join(f"{n:<26}" for n in names))
    print("-" * (18 + 26 * len(names)))

    for endpoint in ("/bg/sync", "/bg/write", "/bg/cpu"):
        row = [f"{endpoint:<18}"]
        completion_info = []
        for name, port in fws:
            before = get_count(port)
            try:
                r = bench(f"{name}:{endpoint}", port, endpoint)
                row.append(f"p50={r['p50']:3}μs rps={r['rps']:6}  ")
                # Give async tasks a moment to complete
                time.sleep(0.3)
                after = get_count(port)
                completion_info.append((name, after - before))
            except Exception as e:
                row.append(f"ERR: {e.__class__.__name__}         ")
        print("".join(row))
        # Completion report
        comp = ", ".join(f"{n}={d}/{N}" for n, d in completion_info)
        print(f"  └ completed: {comp}")


if __name__ == "__main__":
    main()
