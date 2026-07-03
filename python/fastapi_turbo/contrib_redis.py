"""Multiplexed RESP2 Redis client for asyncio — ONE socket per event loop.

Why this exists (measured, benchmarks/matrix):  redis-py's asyncio client
checks a connection out of a pool for every in-flight command, so 64
concurrent HTTP requests across 8 workers = 64 Redis connections, each
carrying exactly one command per round trip (~50k rps).  ioredis (Fastify)
instead interleaves every command over ONE socket per process — 8
connections total at ~99k rps and half the p50.  RESP2 replies arrive in
strict request order on a connection, so multiplexing needs no IDs: a FIFO
of futures is a complete demultiplexer.

Design:
  * ``asyncio.Protocol`` on the running loop; one lazily-created connection
    per event loop (``mux_for_loop()``), so every task on that loop shares it.
  * Writes are coalesced per loop tick: commands append to a pending buffer
    and a single ``call_soon`` flush issues one ``transport.write`` for all
    commands queued in the tick (the syscall batching is where the pool
    model loses).
  * Replies are parsed with ``hiredis.Reader`` when installed (it is an
    optional dependency), else a pure-Python incremental RESP2 parser.

Scope (v1): plain request/response commands only — GET/SET/DEL/INCR/EXPIRE/
arbitrary ``execute(*args)``.  NOT for SUBSCRIBE/PSUBSCRIBE, blocking
commands (BLPOP, BRPOPLPUSH, WAIT...), or stateful MULTI/WATCH — those
pin or push on the connection and belong on a dedicated redis-py client.

Framework-neutral: works identically under fastapi-turbo's per-worker loops
and under uvicorn/plain asyncio (fairness note for benchmarks).
"""
from __future__ import annotations

import asyncio
from collections import deque
from weakref import WeakKeyDictionary

try:  # optional accelerator — pure-Python fallback below
    from hiredis import Reader as _HiReader, ReplyError as _HiReplyError
except ImportError:  # pragma: no cover - exercised via _PyReader tests
    _HiReader = None
    _HiReplyError = None

__all__ = ["RedisMux", "RedisMuxError", "mux_for_loop"]


class RedisMuxError(Exception):
    """Redis returned an error reply (-ERR ...)."""


class _PyReader:
    """Minimal incremental RESP2 parser (fallback when hiredis is absent)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def gets(self):
        parsed = self._parse(0)
        if parsed is None:
            return False
        value, consumed = parsed
        del self._buf[:consumed]
        return value

    def _parse(self, pos: int):
        buf = self._buf
        nl = buf.find(b"\r\n", pos)
        if nl < 0:
            return None
        kind, line = buf[pos:pos + 1], bytes(buf[pos + 1:nl])
        head_end = nl + 2
        if kind == b"+":
            return line, head_end - pos
        if kind == b":":
            return int(line), head_end - pos
        if kind == b"-":
            return RedisMuxError(line.decode()), head_end - pos
        if kind == b"$":
            n = int(line)
            if n == -1:
                return None, head_end - pos
            if len(buf) < head_end + n + 2:
                return None  # incomplete
            return bytes(buf[head_end:head_end + n]), head_end + n + 2 - pos
        if kind == b"*":
            n = int(line)
            if n == -1:
                return None, head_end - pos
            items, cursor = [], head_end
            for _ in range(n):
                item = self._parse(cursor)
                if item is None:
                    return None  # incomplete
                value, consumed = item
                items.append(value)
                cursor += consumed
            return items, cursor - pos
        raise ConnectionError(f"unexpected RESP type byte: {kind!r}")


def _encode_command(args) -> bytes:
    """Encode args as a RESP array of bulk strings."""
    out = bytearray(b"*%d\r\n" % len(args))
    for a in args:
        if isinstance(a, bytes):
            b = a
        elif isinstance(a, str):
            b = a.encode()
        elif isinstance(a, int):
            b = b"%d" % a
        elif isinstance(a, float):
            b = repr(a).encode()
        else:
            raise TypeError(f"unsupported argument type: {type(a).__name__}")
        out += b"$%d\r\n" % len(b)
        out += b
        out += b"\r\n"
    return bytes(out)


class _MuxProtocol(asyncio.Protocol):
    def __init__(self, mux: "RedisMux") -> None:
        self._mux = mux

    def connection_made(self, transport) -> None:
        transport.set_write_buffer_limits(high=1 << 20)

    def data_received(self, data: bytes) -> None:
        mux = self._mux
        reader, waiters = mux._reader, mux._waiters
        reader.feed(data)
        while True:
            reply = reader.gets()
            if reply is False:
                break
            fut = waiters.popleft()
            if fut.done():  # caller cancelled/timed out; drop the reply
                continue
            if _HiReplyError is not None and isinstance(reply, _HiReplyError):
                fut.set_exception(RedisMuxError(*reply.args))
            elif isinstance(reply, RedisMuxError):
                fut.set_exception(reply)
            else:
                fut.set_result(reply)

    def connection_lost(self, exc) -> None:
        self._mux._on_connection_lost(exc)


class RedisMux:
    """One multiplexed RESP2 connection; safe to share across asyncio tasks
    on the same event loop.  Commands issued concurrently are interleaved
    on the single socket and demultiplexed FIFO."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6379,
                 db: int = 0) -> None:
        self._host, self._port, self._db = host, port, db
        self._loop = None
        self._transport = None
        self._reader = None
        self._waiters: deque = deque()
        self._wbuf = bytearray()
        self._flush_scheduled = False
        self._connecting = None  # in-flight connect future, collapses racers
        self._closed = False

    # ── connection management ────────────────────────────────────────
    async def connect(self) -> None:
        if self._transport is not None and not self._transport.is_closing():
            return
        if self._closed:
            raise ConnectionError("RedisMux is closed")
        loop = asyncio.get_running_loop()
        if self._connecting is not None:  # another task is already dialing
            await self._connecting
            if self._transport is None or self._transport.is_closing():
                raise ConnectionError("redis connect failed")
            return
        self._connecting = loop.create_future()
        try:
            transport, _ = await loop.create_connection(
                lambda: _MuxProtocol(self), self._host, self._port)
            self._loop = loop
            self._reader = _HiReader() if _HiReader is not None else _PyReader()
            self._waiters.clear()
            self._wbuf.clear()
            self._flush_scheduled = False
            self._transport = transport
            if self._db:
                await self.execute("SELECT", self._db)
        finally:
            connecting, self._connecting = self._connecting, None
            connecting.set_result(None)

    def _on_connection_lost(self, exc) -> None:
        self._transport = None
        err = ConnectionError(f"redis connection lost: {exc!r}")
        waiters, self._waiters = self._waiters, deque()
        for fut in waiters:
            if not fut.done():
                fut.set_exception(err)

    def close(self) -> None:
        self._closed = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ── command path ─────────────────────────────────────────────────
    def _flush(self) -> None:
        self._flush_scheduled = False
        if self._transport is not None and self._wbuf:
            data = bytes(self._wbuf)
            self._wbuf.clear()
            self._transport.write(data)

    def _send(self, encoded: bytes):
        fut = self._loop.create_future()
        if not self._waiters and not self._wbuf:
            # Idle connection (nothing in flight): write immediately so the
            # command hits the wire before this task even suspends —
            # measurably lower latency for serial callers.
            self._waiters.append(fut)
            self._transport.write(encoded)
            return fut
        # Commands already in flight: coalesce.  The call_soon flush runs
        # after the current batch of ready callbacks, before the loop polls
        # I/O, so every command queued this tick goes out in ONE
        # transport.write (syscall batching is where pools lose).
        self._waiters.append(fut)
        self._wbuf += encoded
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._loop.call_soon(self._flush)
        return fut

    async def execute(self, *args):
        """Send one command, await its reply.  args: str/bytes/int/float."""
        if self._transport is None or self._transport.is_closing():
            await self.connect()
        return await self._send(_encode_command(args))

    # Conveniences for the hot bench/app paths.
    async def get(self, key):
        if self._transport is None or self._transport.is_closing():
            await self.connect()
        return await self._send(
            b"*2\r\n$3\r\nGET\r\n$%d\r\n%s\r\n"
            % (len(k := key.encode() if isinstance(key, str) else key), k))

    async def set(self, key, value):
        if self._transport is None or self._transport.is_closing():
            await self.connect()
        k = key.encode() if isinstance(key, str) else key
        v = value.encode() if isinstance(value, str) else value
        return await self._send(
            b"*3\r\n$3\r\nSET\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n"
            % (len(k), k, len(v), v))


# ── per-event-loop singleton ──────────────────────────────────────────
_per_loop: "WeakKeyDictionary[asyncio.AbstractEventLoop, RedisMux]" = WeakKeyDictionary()


def mux_for_loop(host: str = "127.0.0.1", port: int = 6379,
                 db: int = 0) -> RedisMux:
    """RedisMux shared by everything on the current event loop (call from a
    running loop).  fastapi-turbo runs one loop per worker → one Redis
    connection per worker, the ioredis topology."""
    loop = asyncio.get_running_loop()
    mux = _per_loop.get(loop)
    if mux is None:
        mux = RedisMux(host, port, db)
        _per_loop[loop] = mux
    return mux
