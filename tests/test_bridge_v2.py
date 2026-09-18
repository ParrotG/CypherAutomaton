from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from incypher_bridge.app import create_app
from incypher_bridge.settings import BridgeSettings
from tests.mock_gate import MockGate


async def recv_until(reader: asyncio.StreamReader, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        if not chunk:
            raise AssertionError(f"unexpected EOF: {bytes(data)!r}")
        data.extend(chunk)
    return bytes(data)


class BridgeV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.gate = MockGate(team_key=b"test-team-key", bits=8).start()
        settings = BridgeSettings(
            state_root=Path(self.temp.name) / "bridge",
            team_key="test-team-key",
            env_file="test.env",
            max_handshakes=4,
        )
        settings.state_root.mkdir(parents=True, exist_ok=True)
        self.app = create_app(settings)
        self.transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(
            transport=self.transport,
            base_url="http://bridge.test",
            timeout=10.0,
        )

    async def asyncTearDown(self) -> None:
        await self.app.state.manager.stop_all()
        await self.client.aclose()
        self.gate.stop()
        self.temp.cleanup()

    async def create_ready(self, connector_id: str, *, auto_reconnect: bool = False) -> dict:
        response = await self.client.post(
            "/connectors",
            json={
                "connector_id": connector_id,
                "target": f"{self.gate.host}:{self.gate.port}",
                "auto_reconnect": auto_reconnect,
                "reconnect_delay": 0.05,
                "connect_timeout": 3.0,
                "banner_timeout": 3.0,
                "pow_timeout": 5.0,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        info = response.json()
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            status = await self.client.get(f"/connectors/{connector_id}")
            self.assertEqual(status.status_code, 200, status.text)
            info = status.json()
            if info["state"] == "ready":
                return info
            await asyncio.sleep(0.02)
        self.fail(f"connector did not become ready: {info}")

    async def test_http_api_and_two_connectors_are_independent(self) -> None:
        first = await self.create_ready("agent-a")
        second = await self.create_ready("agent-b")
        self.assertNotEqual(first["bind_port"], second["bind_port"])

        reader_a, writer_a = await asyncio.open_connection(
            first["bind_host"], first["bind_port"]
        )
        reader_b, writer_b = await asyncio.open_connection(
            second["bind_host"], second["bind_port"]
        )
        await recv_until(reader_a, b"MOCK CHALLENGE START")
        await recv_until(reader_b, b"MOCK CHALLENGE START")

        writer_a.write(b"alpha\n")
        await writer_a.drain()
        writer_b.write(b"beta\n")
        await writer_b.drain()
        self.assertIn(b"echo: alpha\n", await recv_until(reader_a, b"echo: alpha\n"))
        self.assertIn(b"echo: beta\n", await recv_until(reader_b, b"echo: beta\n"))

        writer_a.close()
        writer_b.close()
        await writer_a.wait_closed()
        await writer_b.wait_closed()

        listing = await self.client.get("/connectors")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(
            sorted(item["connector_id"] for item in listing.json()["connectors"]),
            ["agent-a", "agent-b"],
        )

        events = await self.client.get("/connectors/agent-a/events")
        self.assertEqual(events.status_code, 200)
        directions = {event["dir"] for event in events.json()["events"]}
        self.assertIn("C->A", directions)
        self.assertIn("A->C", directions)

    async def test_upstream_loss_keeps_connector_endpoint(self) -> None:
        info = await self.create_ready("reconnect-a", auto_reconnect=True)
        reader, writer = await asyncio.open_connection(info["bind_host"], info["bind_port"])
        await recv_until(reader, b"MOCK CHALLENGE START")

        # Ask the mock upstream to close its side; the local agent should see EOF.
        writer.write(b"__close__\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(4096), timeout=3.0), b"")
        writer.close()
        await writer.wait_closed()

        deadline = asyncio.get_running_loop().time() + 5.0
        status = info
        while asyncio.get_running_loop().time() < deadline:
            status = await self.client.get("/connectors/reconnect-a")
            status = status.json()
            if status["state"] == "ready" and status["generation"] >= 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(status["state"], "ready")
        self.assertGreaterEqual(status["generation"], 2)
        self.assertEqual(status["bind_port"], info["bind_port"])

        replacement_reader, replacement_writer = await asyncio.open_connection(
            info["bind_host"], info["bind_port"]
        )
        await recv_until(replacement_reader, b"MOCK CHALLENGE START")
        replacement_writer.write(b"after-reconnect\n")
        await replacement_writer.drain()
        self.assertIn(
            b"echo: after-reconnect\n",
            await recv_until(replacement_reader, b"echo: after-reconnect\n"),
        )
        replacement_writer.close()
        await replacement_writer.wait_closed()

    async def test_duplicate_connector_id_is_rejected(self) -> None:
        await self.create_ready("duplicate")
        response = await self.client.post(
            "/connectors",
            json={
                "connector_id": "duplicate",
                "target": f"{self.gate.host}:{self.gate.port}",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
