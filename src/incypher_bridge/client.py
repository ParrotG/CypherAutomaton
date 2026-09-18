"""HTTP client and local-socket helper for the bridge service."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx


class BridgeAPIError(RuntimeError):
    """Raised when the bridge HTTP API returns an error."""


class BridgeClient:
    """Small async client for the FastAPI bridge control plane."""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def __aenter__(self) -> "BridgeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text
            raise BridgeAPIError(f"{method} {path} -> {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def create_connector(self, **payload: Any) -> dict[str, Any]:
        return await self._request("POST", "/connectors", json=payload)

    async def list_connectors(self) -> dict[str, Any]:
        return await self._request("GET", "/connectors")

    async def get_connector(self, connector_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/connectors/{connector_id}")

    async def reconnect_connector(self, connector_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/connectors/{connector_id}/reconnect")

    async def delete_connector(self, connector_id: str) -> None:
        await self._request("DELETE", f"/connectors/{connector_id}")

    async def get_events(self, connector_id: str, *, limit: int = 200) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/connectors/{connector_id}/events",
            params={"limit": limit},
        )

    async def wait_ready(
        self,
        connector_id: str,
        *,
        timeout: float = 30.0,
        interval: float = 0.2,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        last: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            last = await self.get_connector(connector_id)
            state = last.get("state")
            if state == "ready":
                return last
            if state == "closed":
                raise BridgeAPIError(f"connector {connector_id} closed: {last.get('last_error')}")
            await asyncio.sleep(interval)
        raise TimeoutError(
            f"connector {connector_id} did not become ready in {timeout}s; last={last}"
        )


def connect_local(port: int, host: str = "127.0.0.1", *, timeout: float = 10.0) -> socket.socket:
    """Open a raw socket to a bridge connector endpoint."""

    return socket.create_connection((host, port), timeout=timeout)
