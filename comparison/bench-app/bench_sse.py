"""SSE streaming benchmark — fastapi-turbo vs Go Gin vs Fastify.

Mirrors vLLM / SGLang's per-request SSE stream: N data chunks terminated
with `data: [DONE]\n\n`. Measures:

  - time to last byte  (total request latency, dominated by the N chunks)
  - time to first byte  (how quickly the server flushes chunk 0)
  - requests / sec over a keepalive connection

Servers expected to be up on 19500 (fastapi-turbo), 19501 (Go), 19502 (Fastify).
"""
from __future__ import annotations

import socket
import sys
import time


def _conn(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect(("127.0.0.1", port))
    return s


def _read_until_done(sock: socket.socket, buf: bytearray) -> tuple[float, int]:
    """Drain one SSE response ending in `data: [DONE]\n\n`.

    Returns (time_to_first_byte_us, total_bytes). The initial `buf`
    contains anything already buffered from a prior read.
    """
    start = time.perf_counter()
    ttfb: float | None = None
    terminator = b"data: [DONE]\n\n"
    while terminator not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("connection closed before DONE")
        if ttfb is None:
            ttfb = (time.perf_counter() - start) * 1e6
        buf.extend(chunk)
    # Chunked encoding adds `0\r\n\r\n` after the final data frame — make
    # sure we have consumed it so the next request's headers start clean.
    # (The test is approximate: if there's no chunked trailer we don't
    # hang because we only loop until DONE is in the buffer.)
    end_idx = buf.index(terminator) + len(terminator)
    consumed = bytes(buf[:end_idx])
    del buf[:end_idx]
    # Drain trailing chunked bits (non-blocking best-effort)
    sock.setblocking(False)
    try:
        while True:
            extra = sock.recv(65536)
            if not extra:
                break
    except (BlockingIOError, OSError):
        pass
    sock.setblocking(True)
    return (ttfb if ttfb is not None else 0.0), len(consumed)


def bench_one(port: int, n: int, iters: int, warmup: int) -> dict:
    s = _conn(port)
    buf = bytearray()
    req = (
        f"GET /stream?n={n} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode()
    try:
        for _ in range(warmup):
            # Use a fresh connection each time — SSE responses close or
            # confuse keep-alive on some servers.
            s.close()
            s = _conn(port)
            buf = bytearray()
            s.sendall(req)
            _read_until_done(s, buf)

        tlats: list[float] = []
        ttfbs: list[float] = []
        sizes: list[int] = []
        t0 = time.perf_counter()
        for _ in range(iters):
            s.close()
            s = _conn(port)
            buf = bytearray()
            t = time.perf_counter()
            s.sendall(req)
            ttfb, size = _read_until_done(s, buf)
            tlats.append((time.perf_counter() - t) * 1e6)
            ttfbs.append(ttfb)
            sizes.append(size)
        elapsed = time.perf_counter() - t0
    finally:
        s.close()

    tlats.sort()
    ttfbs.sort()
    return {
        "p50_total": tlats[len(tlats) // 2],
        "p99_total": tlats[int(len(tlats) * 0.99)],
        "p50_ttfb": ttfbs[len(ttfbs) // 2],
        "rps": int(iters / elapsed),
        "bytes": sizes[0],
    }


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    ITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    WARMUP = 200

    cases = [
        ("fastapi-turbo", 19500),
        ("Go Gin",     19501),
        ("Fastify",    19502),
    ]

    print(f"SSE stream bench — n={N} chunks, {ITERS} requests each, single keepalive conn\n")
    print(f"{'Backend':<14}{'p50 total':>12}{'p99 total':>12}{'p50 TTFB':>12}{'req/s':>10}{'bytes':>10}")
    print("-" * 70)
    results = []
    for name, port in cases:
        try:
            r = bench_one(port, N, ITERS, WARMUP)
            results.append((name, r))
        except Exception as e:
            print(f"{name:<14}  ERR: {e}")
    results.sort(key=lambda x: x[1]["p50_total"])
    for name, r in results:
        print(
            f"{name:<14}{int(r['p50_total']):>10}μs"
            f"{int(r['p99_total']):>10}μs"
            f"{int(r['p50_ttfb']):>10}μs"
            f"{r['rps']:>10}"
            f"{r['bytes']:>10}"
        )


if __name__ == "__main__":
    main()
