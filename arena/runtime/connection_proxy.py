"""Small local middleware for sharing one raw-TCP dynamic instance.

Dynamic pwn/network services often allow only one meaningful upstream session.
Each solver sub-agent can still connect to a local TCP endpoint; this proxy
serializes upstream sessions behind a lock and solves the official PoW gate.
"""

from __future__ import annotations

import re
import select
import socket
import socketserver
import threading
import time
from typing import Type

_RAW_RE = re.compile(r"\bnc\s+([^\s]+)\s+(\d+)")
_KEY_RE = re.compile(r"team_key=([^\s)]+)")


def parse_raw_connection(info: str | None) -> tuple[str, int, str | None, bool] | None:
    """Return (host, port, team_key, pow_required) for an official nc string."""
    if not info:
        return None
    match = _RAW_RE.search(info)
    if not match:
        return None
    host = match.group(1)
    try:
        port = int(match.group(2))
    except ValueError:
        return None
    key_match = _KEY_RE.search(info)
    team_key = key_match.group(1) if key_match else None
    return host, port, team_key, "pow" in info.lower()


def _connect_upstream(host: str, port: int, team_key: str | None, pow_required: bool) -> socket.socket:
    if pow_required and team_key:
        from ..official.ctfd import connect_pwn

        return connect_pwn(host, port, team_key)
    sock = socket.create_connection((host, int(port)), timeout=30)
    sock.settimeout(None)
    return sock


def _relay(client: socket.socket, upstream: socket.socket, idle_timeout: float) -> None:
    sockets = [client, upstream]
    last_activity = time.monotonic()
    while True:
        readable, _, _ = select.select(sockets, [], [], 1.0)
        if not readable:
            if idle_timeout > 0 and time.monotonic() - last_activity > idle_timeout:
                return
            continue
        for source in readable:
            try:
                data = source.recv(65536)
            except Exception:
                return
            if not data:
                return
            target = upstream if source is client else client
            try:
                target.sendall(data)
            except Exception:
                return
            last_activity = time.monotonic()


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    # Filled in by RawTCPProxy.start().
    upstream_host: str = ""
    upstream_port: int = 0
    team_key: str | None = None
    pow_required: bool = False
    upstream_lock: threading.Lock
    idle_timeout: float = 600.0


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: D102
        server = self.server
        with server.upstream_lock:
            try:
                upstream = _connect_upstream(
                    server.upstream_host,
                    server.upstream_port,
                    server.team_key,
                    server.pow_required,
                )
            except Exception:
                return
            try:
                _relay(self.request, upstream, server.idle_timeout)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass


class RawTCPProxy:
    """Serialize upstream access and expose one local nc endpoint."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        team_key: str | None = None,
        pow_required: bool = False,
        idle_timeout: float = 600.0,
        server_class: Type[_ThreadingServer] = _ThreadingServer,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.team_key = team_key
        self.pow_required = pow_required
        self.idle_timeout = idle_timeout
        self._server_class = server_class
        self._server: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        server = self._server_class(("127.0.0.1", 0), _ProxyHandler)
        server.upstream_host = self.host
        server.upstream_port = self.port
        server.team_key = self.team_key
        server.pow_required = self.pow_required
        server.upstream_lock = threading.Lock()
        server.idle_timeout = self.idle_timeout
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="raw-tcp-proxy",
            daemon=True,
        )
        self._thread.start()
        local_port = int(server.server_address[1])
        return f"nc 127.0.0.1 {local_port}  (local serialized gateway; PoW handled by proxy)"

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None
