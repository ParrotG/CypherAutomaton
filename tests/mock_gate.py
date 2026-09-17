"""Local stand-in for the IN-CYPHER PoW gate used by the test suite."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socketserver
import threading
from typing import Type


def _leading_zero_bits_ok(digest: bytes, bits: int) -> bool:
    full, remaining = divmod(bits, 8)
    if any(byte != 0 for byte in digest[:full]):
        return False
    if remaining and digest[full] >> (8 - remaining):
        return False
    return True


def _recv_line(conn) -> bytes:
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = conn.recv(1)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def make_handler(team_key: bytes, bits: int) -> Type[socketserver.BaseRequestHandler]:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            conn = self.request
            nonce = secrets.token_hex(16)
            prompt = (
                "IN-CYPHER pwn instance\n"
                "proof-of-work required (team-key bound)\n"
                f"nonce={nonce} bits={bits}\n"
                "send X such that sha256(hmac_sha256(team_key, nonce) || X) "
                f"has {bits} leading zero bits:\n"
            ).encode()
            conn.sendall(prompt)
            x = _recv_line(conn).strip()
            mac = hmac.new(team_key, nonce.encode(), hashlib.sha256).digest()
            if _leading_zero_bits_ok(hashlib.sha256(mac + x).digest(), bits):
                conn.sendall(b"proof accepted\nMOCK CHALLENGE START\n> ")
                while True:
                    line = _recv_line(conn)
                    if not line:
                        break
                    if line == b"__close__\n":
                        return
                    conn.sendall(b"echo: " + line)
            else:
                conn.sendall(b"denied: invalid proof (wrong team key or insufficient work)\n")

    return Handler


class MockGate:
    """A running mock gate bound to an ephemeral or supplied TCP port."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        team_key: bytes = b"test-team-key",
        bits: int = 12,
    ) -> None:
        handler = make_handler(team_key, bits)
        self._server = socketserver.ThreadingTCPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "MockGate":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
