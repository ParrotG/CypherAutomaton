"""Opt-in live bridge probe using plain TCP clients, with no model or agent.

Run from the repository root with python -m tests.bridge_connection_probe.
Only PoW proofs are sent upstream; challenge output is read without commands.
"""

from __future__ import annotations

import argparse
import json
import socket
import tempfile
import time
from pathlib import Path

from incypher_bridge.config import load_team_key
from incypher_bridge.control import ControlServer, request
from incypher_bridge.manager import SessionManager
from incypher_bridge.paths import RunPaths
from incypher_bridge.pow import DEFAULT_VARIANTS
from incypher_bridge.session import BridgeConfig, BridgeSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--env-file", default=".env.local")
    args = parser.parse_args()
    if not 1 <= args.sessions <= 4:
        parser.error("--sessions must be between 1 and 4")
    key = load_team_key(env_file=args.env_file)
    with tempfile.TemporaryDirectory(prefix="bridge-probe-") as temp:
        paths = RunPaths.create(Path(temp), challenge_id="connection-probe", run_id="primary")
        primary = BridgeSession(BridgeConfig(
            host=args.host, port=args.port, team_key=key, duration=120,
            auto_reconnect=False, pow_timeout=30, variants=DEFAULT_VARIANTS[:1],
            challenge_id="connection-probe", run_id="primary",
        ), paths)
        manager = SessionManager(primary, max_sessions=args.sessions, max_handshakes=1)
        control = ControlServer(primary, paths.control_socket, manager=manager)
        primary.add_shutdown_hook(control.shutdown)
        clients = []
        try:
            control.start()
            primary.start()
            for index in range(1, args.sessions):
                response = request(paths.control_socket, "session-create", run_id=f"probe-{index}")
                if not response["ok"]:
                    raise RuntimeError(response["error"])
            deadline = time.monotonic() + 100
            while True:
                statuses = request(paths.control_socket, "sessions")["sessions"]
                if any(status["state"] in ("stopped", "upstream_closed") for status in statuses):
                    raise RuntimeError("A session failed or closed before all sessions were ready")
                if all(status["state"] == "ready" and status["bind_port"] for status in statuses):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Sessions did not become ready")
                time.sleep(0.1)
            nonces = {status["upstream_meta"]["nonce"] for status in statuses}
            if len(nonces) != args.sessions:
                raise AssertionError("Expected an independent PoW nonce for each session")
            for status in statuses:
                client = socket.create_connection(("127.0.0.1", status["bind_port"]), timeout=3)
                clients.append(client)
            received = [0] * len(clients)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                for index, client in enumerate(clients):
                    client.settimeout(0.1)
                    try:
                        data = client.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        raise AssertionError("A concurrent client received EOF")
                    received[index] += len(data)
            statuses = request(paths.control_socket, "sessions")["sessions"]
            if not all(status["client_connected"] and status["state"] == "ready" for status in statuses):
                raise AssertionError("Concurrent sessions did not remain connected")
            print(json.dumps({
                "ok": True, "target": f"{args.host}:{args.port}",
                "concurrent_sessions": len(clients), "independent_pow_nonces": len(nonces),
                "received_bytes": received, "challenge_commands_sent": 0,
            }))
        finally:
            for client in clients:
                client.close()
            primary.stop("connection probe complete")
            for worker in manager._workers:
                worker.join(15)


if __name__ == "__main__":
    main()
