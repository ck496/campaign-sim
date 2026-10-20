"""Async UNIX socket library: newline-delimited JSON over stream sockets."""
from __future__ import annotations

import asyncio
import collections
import json
import os
import socket
import struct
import time as _time
from typing import Any, Callable, Iterable, Optional, Set


# --- constants ---
# Framing and protection limits.

MAX_LINE: int = 4096
DRAIN_TIMEOUT: float = 5.0
RATE_LIMIT: int = 20


# --- connection ---
# Wraps an asyncio reader/writer pair with per-connection state.

class Conn:
    """One connection: reader, writer, rate-limit timestamps."""
    __slots__ = (
        "reader", "writer", "_timestamps", "_closed", "_wlock",
    )

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._timestamps: collections.deque = collections.deque()
        self._closed: bool = False
        self._wlock: asyncio.Lock = asyncio.Lock()

    def close(self) -> None:
        """Close the underlying writer if not already closed."""
        if not self._closed:
            self._closed = True
            try:
                self.writer.close()
            except Exception:
                pass


# --- module state ---
# All live server-side connections; used by broadcast().

_connections: Set[Conn] = set()
_on_disconnect_cb: Optional[Callable] = None


# --- framing ---
# Encode and decode newline-delimited JSON.

def _encode(obj: Any) -> bytes:
    """Serialize obj as JSON + newline."""
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


# --- sending ---
# Unicast, multicast, broadcast with drain timeout.

async def _safe_write(conn: Conn, data: bytes) -> bool:
    """Write data to conn with drain timeout. Returns True on success."""
    if conn._closed:
        return False
    async with conn._wlock:
        if conn._closed:
            return False
        try:
            conn.writer.write(data)
            await asyncio.wait_for(conn.writer.drain(), DRAIN_TIMEOUT)
            return True
        except Exception:
            conn.close()
            return False


async def send(conn: Conn, obj: Any) -> None:
    """Send one JSON message to a connection."""
    data = _encode(obj)
    ok = await _safe_write(conn, data)
    if not ok and conn in _connections:
        _connections.discard(conn)
        if _on_disconnect_cb is not None:
            await _on_disconnect_cb(conn)


async def multicast(conns: Iterable[Conn], obj: Any) -> None:
    """Send one JSON message to a group of connections."""
    data = _encode(obj)
    targets = list(conns)

    async def _send_one(c: Conn) -> None:
        ok = await _safe_write(c, data)
        if not ok and c in _connections:
            _connections.discard(c)
            if _on_disconnect_cb is not None:
                await _on_disconnect_cb(c)

    await asyncio.gather(
        *(_send_one(c) for c in targets), return_exceptions=True,
    )


async def broadcast(obj: Any) -> None:
    """Send one JSON message to every live connection."""
    await multicast(set(_connections), obj)


# --- server ---
# Listener, read loop, callbacks.

async def serve(
    path: str,
    on_connect: Callable,
    on_message: Callable,
    on_disconnect: Callable,
    mode: int = 0o660,
) -> asyncio.AbstractServer:
    """Start a UNIX stream socket server at path."""
    global _on_disconnect_cb
    _on_disconnect_cb = on_disconnect

    # remove stale socket if it exists
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    async def _client_handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        conn = Conn(reader, writer)
        _connections.add(conn)
        try:
            await on_connect(conn)
            await _read_loop(conn, on_message)
        finally:
            _connections.discard(conn)
            conn.close()
            try:
                await on_disconnect(conn)
            except Exception:
                pass

    server = await asyncio.start_unix_server(_client_handler, path=path)
    os.chmod(path, mode)
    return server


async def _read_loop(conn: Conn, on_message: Callable) -> None:
    """Read lines from conn, dispatch to on_message or send errors."""
    while not conn._closed:
        try:
            line = await conn.reader.readline()
        except Exception:
            break
        if not line:
            # EOF
            break
        # oversize line: close the connection
        if len(line) > MAX_LINE:
            conn.close()
            break
        # parse JSON
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # bad JSON: send error, keep connection open
            err = _encode({"t": "error", "reason": "bad JSON"})
            try:
                conn.writer.write(err)
                await conn.writer.drain()
            except Exception:
                break
            continue
        # rate limit: sliding window
        now = _time.monotonic()
        conn._timestamps.append(now)
        while conn._timestamps and conn._timestamps[0] < now - 1.0:
            conn._timestamps.popleft()
        if len(conn._timestamps) > RATE_LIMIT:
            conn.close()
            break
        # dispatch
        result = on_message(conn, msg)
        if asyncio.iscoroutine(result):
            await result


# --- client ---
# Connect and receive.

async def connect(path: str) -> Conn:
    """Open a client connection to a UNIX stream socket."""
    reader, writer = await asyncio.open_unix_connection(path)
    return Conn(reader, writer)


async def recv(conn: Conn) -> Optional[dict]:
    """Read one JSON message from a connection. None on EOF."""
    try:
        line = await conn.reader.readline()
    except Exception:
        return None
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# --- identity ---
# Kernel-verified uid via SO_PEERCRED on Linux; None elsewhere.

def peer_uid(conn: Conn) -> Optional[int]:
    """Return the uid of the peer process, or None if unavailable."""
    sock = conn.writer.get_extra_info("socket")
    if sock is None:
        return None
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        cred = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"),
        )
        _pid, uid, _gid = struct.unpack("iII", cred)
        return uid
    except (OSError, struct.error):
        return None
