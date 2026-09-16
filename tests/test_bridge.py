from __future__ import annotations

import socket
import tempfile
import time
import unittest
from pathlib import Path

from incypher_bridge.control import ControlServer, request as control_request
from incypher_bridge.session import BridgeConfig, BridgeSession
from incypher_bridge.paths import RunPaths
from solver import connect_bridge

from tests.mock_gate import MockGate


class BridgeTests(unittest.TestCase):
    def test_end_to_end_local_bridge(self) -> None:
        gate = MockGate(team_key=b"test-team-key", bits=10).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                state_dir = Path(temp)
                paths = RunPaths.create(
                    state_dir,
                    challenge_id="test-challenge",
                    run_id="test-run",
                )
                config = BridgeConfig(
                    host=gate.host,
                    port=gate.port,
                    team_key="test-team-key",
                    duration=5.0,
                    bind_host="127.0.0.1",
                    bind_port=0,
                    auto_reconnect=False,
                    pow_timeout=30.0,
                    verbose=False,
                    challenge_id="test-challenge",
                    run_id="test-run",
                    description_text="test description",
                )
                self.assertLess(len(str(paths.control_socket)), 107)
                session = BridgeSession(config, paths)
                control = ControlServer(session, paths.control_socket)
                session.add_shutdown_hook(control.shutdown)
                control.start()
                try:
                    session.start()
                    status = session.status()
                    self.assertEqual(status["state"], "ready")
                    self.assertIsNotNone(status["bind_port"])
                    self.assertEqual(status["challenge_id"], "test-challenge")
                    self.assertEqual(status["run_id"], "test-run")
                    self.assertEqual(status["description_text"], "test description")

                    client = connect_bridge(int(status["bind_port"]))
                    client.settimeout(3.0)
                    greeting = b""
                    deadline = time.monotonic() + 3.0
                    while b"MOCK CHALLENGE START" not in greeting and time.monotonic() < deadline:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        greeting += chunk
                    self.assertIn(b"proof accepted", greeting)
                    self.assertIn(b"MOCK CHALLENGE START", greeting)

                    client.sendall(b"hello\n")
                    echoed = b""
                    deadline = time.monotonic() + 3.0
                    while b"echo: hello\n" not in echoed and time.monotonic() < deadline:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        echoed += chunk
                    self.assertIn(b"echo: hello\n", echoed)
                    client.close()

                    live = control_request(paths.control_socket, "status")
                    self.assertTrue(live["ok"])
                    self.assertIn("status", live)
                    human = control_request(
                        paths.control_socket,
                        "send",
                        text="status\n",
                        label="test",
                    )
                    self.assertEqual(human["ok"], True, human)
                finally:
                    session.stop(reason="test complete")
                    control.shutdown()
        finally:
            gate.stop()


if __name__ == "__main__":
    unittest.main()
