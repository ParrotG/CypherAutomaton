from __future__ import annotations

import socket
import socketserver
import threading
import time
import unittest

from arena.runtime.connection_proxy import RawTCPProxy, parse_raw_connection


class _EchoHandler(socketserver.BaseRequestHandler):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def handle(self) -> None:  # noqa: D102
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    return
                time.sleep(0.05)
                self.request.sendall(data)
        finally:
            with self.lock:
                type(self).active -= 1


class RawTCPProxyTests(unittest.TestCase):
    def test_parse_raw_connection(self) -> None:
        parsed = parse_raw_connection("nc 1.2.3.4 1234 (PoW-gated: team_key=abc)")
        self.assertEqual(parsed, ("1.2.3.4", 1234, "abc", True))
        self.assertIsNone(parse_raw_connection("https://example.com/"))
        self.assertIsNone(parse_raw_connection(None))

    def test_proxy_serializes_upstream_connections(self) -> None:
        _EchoHandler.active = 0
        _EchoHandler.max_active = 0
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy = RawTCPProxy(server.server_address[0], server.server_address[1])
        endpoint = proxy.start()
        local_port = int(endpoint.split()[2])
        received: list[bytes] = []

        def client() -> None:
            sock = socket.create_connection(("127.0.0.1", local_port), timeout=5)
            try:
                sock.sendall(b"hello")
                received.append(sock.recv(5))
            finally:
                sock.close()

        try:
            clients = [threading.Thread(target=client) for _ in range(2)]
            for item in clients:
                item.start()
            for item in clients:
                item.join(timeout=5)
        finally:
            proxy.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(sorted(received), [b"hello", b"hello"])
        self.assertEqual(_EchoHandler.max_active, 1)


if __name__ == "__main__":
    unittest.main()
