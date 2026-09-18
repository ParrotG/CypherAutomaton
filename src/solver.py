"""Direct solver helper for IN-CYPHER raw-TCP challenges.

The bridge service is the recommended interface for autonomous agents.  This
module remains available for direct, single-use connections::

    from solver import connect

    s = connect("47.236.162.54", 30068)  # team key loaded from .env.local
    print(s.recv(4096))

For multi-agent use, prefer the FastAPI bridge service and connect to the
``endpoint`` returned by ``POST /connectors``.
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path

from incypher_bridge.client import connect_local
from incypher_bridge.config import load_team_key
from incypher_bridge.pow import parse_pow_challenge, solve_pow

__all__ = ["connect", "connect_bridge"]

_BANNER_RE = re.compile(rb"nonce=[0-9a-fA-F]+\s+bits=\d+")


def _recv_until_banner(sock: socket.socket, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while _BANNER_RE.search(data) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for PoW banner")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("remote closed before PoW banner")
        data.extend(chunk)
        if len(data) > 131_072:
            raise ConnectionError("PoW banner exceeded 128 KiB")
    return bytes(data)


def connect(
    host: str,
    port: int,
    team_key: str | bytes | None = None,
    *,
    connect_timeout: float = 10.0,
    banner_timeout: float = 10.0,
    pow_timeout: float = 120.0,
) -> socket.socket:
    """Connect to a raw-TCP challenge and clear its PoW gate."""

    if team_key is None:
        env_file = Path.cwd() / ".env.local"
        if not env_file.exists():
            project_env = Path(__file__).resolve().parents[1] / ".env.local"
            if project_env.exists():
                env_file = project_env
        team_key = load_team_key(env_file=env_file)
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    try:
        banner = _recv_until_banner(sock, timeout=banner_timeout)
        challenge = parse_pow_challenge(banner)
        x = solve_pow(challenge, team_key, timeout=pow_timeout)
        sock.sendall(x + b"\n")
        return sock
    except BaseException:
        sock.close()
        raise


def connect_bridge(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> socket.socket:
    """Connect to a local bridge connector endpoint."""

    return connect_local(port, host, timeout=timeout)
