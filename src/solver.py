"""Temporary drop-in replacement for the promised IN-CYPHER helper suite.

Direct raw-TCP use (official ``solver.connect`` shape)::

    from solver import connect
    s = connect("47.236.162.54", 30214, team_key)  # clears PoW, returns socket

If the temporary bridge daemon is running, agents should instead connect to
the local endpoint printed by ``python -m incypher_bridge serve``.  Use::

    from solver import connect_bridge
    s = connect_bridge(54321)  # local, no team key and no PoW
"""

from __future__ import annotations

import socket

from incypher_bridge.client import PrefixedSocket, connect, connect_local

__all__ = ["connect", "connect_bridge", "PrefixedSocket"]


def connect_bridge(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> socket.socket:
    """Connect to the local bridge listener created by the middleware."""

    return connect_local(port, host, timeout=timeout)
