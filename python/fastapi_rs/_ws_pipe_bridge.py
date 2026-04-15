"""Pipe-based WebSocket bridge for zero-GIL message signaling.

Architecture:
  Rust reader task writes message to pipe (no GIL needed for the write)
  asyncio watches pipe via loop.add_reader() (no GIL needed for the signal)
  When pipe is readable, Python drains it and pushes to asyncio.Queue
"""
import asyncio
import os
import struct


class PipeBridge:
    """Bridges a Unix pipe to an asyncio.Queue for zero-GIL message delivery."""

    def __init__(self, read_fd: int, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._read_fd = read_fd
        self._queue = queue
        self._loop = loop
        self._buf = b""

    def start(self):
        """Register the pipe reader with asyncio."""
        self._loop.add_reader(self._read_fd, self._on_readable)

    def stop(self):
        """Unregister the pipe reader."""
        try:
            self._loop.remove_reader(self._read_fd)
        except Exception:
            pass
        try:
            os.close(self._read_fd)
        except Exception:
            pass

    def _on_readable(self):
        """Called by asyncio when the pipe has data. Runs on the event loop thread."""
        try:
            data = os.read(self._read_fd, 65536)
        except OSError:
            self._queue.put_nowait(None)  # Signal close
            return

        if not data:
            self._queue.put_nowait(None)
            return

        # Protocol: [4-byte length][message bytes][4-byte length][message bytes]...
        self._buf += data
        while len(self._buf) >= 4:
            msg_len = struct.unpack("!I", self._buf[:4])[0]
            if msg_len == 0xFFFFFFFF:  # Sentinel for close
                self._queue.put_nowait(None)
                self._buf = b""
                return
            if len(self._buf) < 4 + msg_len:
                break  # Partial message, wait for more
            msg = self._buf[4:4 + msg_len].decode("utf-8", errors="replace")
            self._buf = self._buf[4 + msg_len:]
            self._queue.put_nowait(msg)
