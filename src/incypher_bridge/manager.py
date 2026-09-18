"""Async connector registry for the bridge service."""

from __future__ import annotations

import asyncio
import re

from .connector import Connector, ConnectorError, parse_target
from .models import ConnectorCreate
from .settings import BridgeSettings

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ConnectorManager:
    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_handshakes)
        self._connectors: dict[str, Connector] = {}
        self._lock = asyncio.Lock()

    async def create(self, spec: ConnectorCreate) -> Connector:
        if spec.connector_id is not None and not _ID_RE.fullmatch(spec.connector_id):
            raise ConnectorError(
                "connector_id must match [A-Za-z0-9_.-]{1,64}"
            )
        parse_target(spec.target)
        async with self._lock:
            connector_id = spec.connector_id
            if connector_id is None:
                import uuid

                connector_id = f"c-{uuid.uuid4().hex[:10]}"
                spec = spec.model_copy(update={"connector_id": connector_id})
            if connector_id in self._connectors:
                raise ConnectorError(f"connector already exists: {connector_id}")
            connector = Connector(
                spec,
                host=spec.bind_host,
                port=spec.bind_port,
                team_key=self.settings.team_key,
                state_root=self.settings.connectors_root,
                handshake_semaphore=self._semaphore,
            )
            await connector.start()
            self._connectors[connector.connector_id] = connector
            return connector

    def get(self, connector_id: str) -> Connector:
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise ConnectorError(f"connector not found: {connector_id}") from exc

    def list(self) -> list[Connector]:
        return list(self._connectors.values())

    async def stop(self, connector_id: str) -> None:
        async with self._lock:
            connector = self._connectors.pop(connector_id, None)
        if connector is None:
            raise ConnectorError(f"connector not found: {connector_id}")
        await connector.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            connectors = list(self._connectors.values())
            self._connectors.clear()
        await asyncio.gather(
            *(connector.stop() for connector in connectors),
            return_exceptions=True,
        )
