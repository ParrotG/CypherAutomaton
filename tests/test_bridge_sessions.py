"""Exercise multiple bridge sessions using sockets, without worker agents."""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
import io
import json
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from incypher_bridge.client import PrefixedSocket
from incypher_bridge.cli import main as cli_main
from incypher_bridge.control import ControlServer, request
from incypher_bridge.manager import SessionManager
from incypher_bridge.paths import RunPaths
from incypher_bridge.session import BridgeConfig, BridgeSession
from tests.mock_gate import MockGate


def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Condition did not become true before timeout")


def receive_until(sock, marker):
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"Unexpected EOF: {data!r}")
        data += chunk
    return data


class MultiSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gate = MockGate(bits=8).start()
        paths = RunPaths.create(Path(self.temp.name), challenge_id="parallel", run_id="primary")
        self.primary = BridgeSession(BridgeConfig(
            host=self.gate.host, port=self.gate.port, team_key="test-team-key",
            duration=None, auto_reconnect=False, challenge_id="parallel", run_id="primary",
        ), paths)
        self.manager = SessionManager(self.primary, max_sessions=3, max_handshakes=1)
        self.control = ControlServer(self.primary, paths.control_socket, manager=self.manager).start()
        self.primary.add_shutdown_hook(self.control.shutdown)
        self.primary.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.primary.stop("test complete")
        for worker in self.manager._workers:
            worker.join(5)
        self.gate.stop()
        self.temp.cleanup()

    def create(self, name):
        response = request(self.primary.paths.control_socket, "session-create", run_id=name)
        self.assertTrue(response["ok"], response)
        child = self.manager._sessions[name]
        until(lambda: child.status()["state"] == "ready" and child.status()["bind_port"])
        return child

    def attach(self, session):
        client = socket.create_connection(("127.0.0.1", session.status()["bind_port"]), timeout=3)
        self.clients.append(client)
        receive_until(client, b"> ")
        return client

    def test_parallel_streams_logs_capacity_and_idempotency(self):
        a, b = self.create("worker-a"), self.create("worker-b")
        sessions = [self.primary, a, b]
        clients = [self.attach(session) for session in sessions]
        barrier = threading.Barrier(3)

        def exchange(index):
            barrier.wait()
            for step in range(10):
                payload = f"worker-{index}-message-{step}\n".encode()
                clients[index].sendall(payload)
                self.assertEqual(receive_until(clients[index], b"\n"), b"echo: " + payload)

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(exchange, range(3)))
        for index, session in enumerate(sessions):
            raw = session.paths.agent_to_challenge_file.read_bytes()
            self.assertEqual(raw, b"".join(f"worker-{index}-message-{n}\n".encode() for n in range(10)))
        duplicate = request(self.primary.paths.control_socket, "session-create", run_id="worker-a")
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["status"]["bind_port"], a.status()["bind_port"])
        self.assertEqual(a.status()["upstream_generation"], 1)
        rejected = request(self.primary.paths.control_socket, "session-create", run_id="overflow")
        self.assertFalse(rejected["ok"])
        self.assertIn("capacity", rejected["error"])
        extra = socket.create_connection(("127.0.0.1", a.status()["bind_port"]), timeout=3)
        with extra:
            self.assertIn(b"busy", extra.recv(4096))
            self.assertEqual(extra.recv(4096), b"")
        request(a.paths.control_socket, "stop")
        until(lambda: a.wait(0))
        self.create("replacement")
        listing = request(self.primary.paths.control_socket, "sessions")
        self.assertEqual(len(listing["sessions"]), 4)

    def test_reattach_preserves_upstream_and_manual_reconnect_isolated(self):
        a, b = self.create("worker-a"), self.create("worker-b")
        ca, cb = self.attach(a), self.attach(b)
        ca.close()
        until(lambda: not a.status()["client_connected"])
        request(a.paths.control_socket, "send", text="buffered\n")
        until(lambda: a.status()["pending_bytes"] > 0)
        ca = socket.create_connection(("127.0.0.1", a.status()["bind_port"]), timeout=3)
        self.clients.append(ca)
        self.assertEqual(receive_until(ca, b"\n"), b"echo: buffered\n")
        self.assertEqual(a.status()["upstream_generation"], 1)
        old_nonce = a.status()["upstream_meta"]["nonce"]
        self.assertTrue(request(a.paths.control_socket, "reconnect")["ok"])
        self.assertEqual(ca.recv(4096), b"")
        until(lambda: a.status()["upstream_generation"] == 2)
        self.assertNotEqual(a.status()["upstream_meta"]["nonce"], old_nonce)
        cb.sendall(b"still alive\n")
        self.assertEqual(receive_until(cb, b"\n"), b"echo: still alive\n")
        self.assertEqual(b.status()["upstream_generation"], 1)
        self.attach(a)
        self.primary.stop("shutdown all")
        self.assertTrue(a.stopped and b.stopped)
        self.assertEqual(cb.recv(4096), b"")
        self.assertFalse(a.paths.control_socket.exists())

    def test_handshake_limit_and_stop_during_creation(self):
        entered = threading.Event()
        release = threading.Event()
        count = 0
        peers = []

        def delayed_connect(*args, **kwargs):
            nonlocal count
            count += 1
            entered.set()
            release.wait(5)
            left, right = socket.socketpair()
            peers.append(right)
            return PrefixedSocket(left)

        with patch("incypher_bridge.session.connect", side_effect=delayed_connect):
            try:
                self.manager.create_session("slow-a")
                self.assertTrue(entered.wait(2))
                self.manager.create_session("slow-b")
                time.sleep(0.15)
                self.assertEqual(count, 1)
                self.primary.stop("cancel startup")
            finally:
                release.set()
                for worker in self.manager._workers:
                    worker.join(3)
                for peer in peers:
                    peer.settimeout(1)
                    self.assertEqual(peer.recv(1), b"")
                    peer.close()
        self.assertEqual(count, 1)
        for child in self.manager._sessions.values():
            self.assertEqual(child.status()["state"], "stopped")
            self.assertIsNone(child._upstream)

    def test_remote_eof_reconnects_only_affected_session(self):
        a = self.create("worker-a")
        a.config.auto_reconnect = True
        a.config.reconnect_delay = 0.05
        ca, primary_client = self.attach(a), self.attach(self.primary)
        ca.sendall(b"__close__\n")
        self.assertEqual(ca.recv(4096), b"")
        until(lambda: a.status()["upstream_generation"] == 2)
        replacement = self.attach(a)
        replacement.sendall(b"new session\n")
        self.assertEqual(receive_until(replacement, b"\n"), b"echo: new session\n")
        primary_client.sendall(b"original session\n")
        self.assertEqual(receive_until(primary_client, b"\n"), b"echo: original session\n")
        self.assertEqual(self.primary.status()["upstream_generation"], 1)

    def test_immediate_eof_during_reconnect_does_not_lose_retry(self):
        child = self.create("worker-a")
        child.config.auto_reconnect = True
        child.config.reconnect_delay = 0.05
        original_open = child._open_connection
        original_adopt = child._adopt_upstream
        original_loss = child._handle_upstream_loss
        loss_finished = threading.Event()
        calls = 0

        def open_connection():
            nonlocal calls
            calls += 1
            if calls == 1:
                left, right = socket.socketpair()
                right.close()
                return PrefixedSocket(left)
            return original_open()

        def handle_loss(sock, generation):
            original_loss(sock, generation)
            if generation == 2:
                loss_finished.set()

        def adopt(sock):
            original_adopt(sock)
            if child.status()["upstream_generation"] == 2:
                loss_finished.wait(3)

        with patch.object(child, "_open_connection", side_effect=open_connection), \
             patch.object(child, "_adopt_upstream", side_effect=adopt), \
             patch.object(child, "_handle_upstream_loss", side_effect=handle_loss):
            child.request_reconnect()
            until(lambda: child.status()["upstream_generation"] == 3)
            self.attach(child)
        self.assertEqual(calls, 2)

    def test_failed_handshake_releases_capacity_and_duplicate_does_not_restart(self):
        with patch("incypher_bridge.session.connect", side_effect=OSError("simulated gate failure")):
            self.manager.create_session("failed")
            child = self.manager._sessions["failed"]
            until(lambda: child.wait(0))
        self.assertIn("simulated gate failure", child.status()["last_error"])
        response = self.manager.create_session("failed")
        self.assertFalse(response["created"])
        self.assertEqual(response["status"]["state"], "stopped")
        self.create("healthy-a")
        self.create("healthy-b")

    def test_concurrent_duplicate_create_has_one_upstream(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.manager.create_session, ["shared-id"] * 4))
        self.assertEqual(sum(result["created"] for result in results), 1)
        child = self.manager._sessions["shared-id"]
        until(lambda: child.status()["state"] == "ready")
        self.assertEqual(child.status()["upstream_generation"], 1)

    def test_reject_invalid_and_existing_run_ids(self):
        for name in ("", "../escape", "x" * 65):
            response = request(self.primary.paths.control_socket, "session-create", run_id=name)
            self.assertFalse(response["ok"])
        old = self.primary.paths.runs_dir / "old-run"
        old.mkdir()
        response = request(self.primary.paths.control_socket, "session-create", run_id="old-run")
        self.assertFalse(response["ok"])

    def test_cli_session_management_and_child_selectors(self):
        def invoke(command, run_id, *extra):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main([
                    command, "--state-dir", self.temp.name,
                    "--challenge-id", "parallel", "--run-id", run_id, *extra,
                ])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())

        created = invoke("session-create", "primary", "--new-run-id", "cli-child")
        self.assertTrue(created["created"])
        child = self.manager._sessions["cli-child"]
        until(lambda: child.status()["state"] == "ready")
        self.assertEqual(len(invoke("sessions", "primary")["sessions"]), 2)
        self.assertTrue(invoke("stop", "cli-child")["ok"])
        until(lambda: child.wait(0))
        self.assertFalse(self.primary.stopped)


if __name__ == "__main__":
    unittest.main()
