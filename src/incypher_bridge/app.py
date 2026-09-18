"""FastAPI control plane for the IN-CYPHER bridge service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response

from .connector import ConnectorError
from .manager import ConnectorManager
from .models import (
    ConnectorCreate,
    ConnectorInfo,
    ConnectorList,
    EventList,
    HealthResponse,
    ReconnectResponse,
)
from .settings import BridgeSettings, SettingsError, load_settings


def create_app(settings: BridgeSettings | None = None) -> FastAPI:
    if settings is None:
        try:
            settings = load_settings()
        except SettingsError as exc:  # pragma: no cover - protected by CLI in practice
            raise RuntimeError(f"bridge settings error: {exc}") from exc

    manager = ConnectorManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await manager.stop_all()

    app = FastAPI(
        title="IN-CYPHER Bridge",
        version="2.0.0",
        description=(
            "Native multi-agent raw-TCP bridge with PoW handling, stable local "
            "connector endpoints, and upstream generation management."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.manager = manager

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(ok=True, connectors=len(manager.list()))

    @app.post("/connectors", response_model=ConnectorInfo, status_code=201)
    async def create_connector(payload: ConnectorCreate) -> dict[str, Any]:
        try:
            connector = await manager.create(payload)
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return connector.info()

    @app.get("/connectors", response_model=ConnectorList)
    async def list_connectors() -> dict[str, Any]:
        return {"connectors": [connector.info() for connector in manager.list()]}

    @app.get("/connectors/{connector_id}", response_model=ConnectorInfo)
    async def get_connector(connector_id: str) -> dict[str, Any]:
        try:
            return manager.get(connector_id).info()
        except ConnectorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/connectors/{connector_id}/events", response_model=EventList)
    async def get_events(
        connector_id: str,
        limit: int = Query(default=200, ge=1, le=10_000),
    ) -> dict[str, Any]:
        try:
            connector = manager.get(connector_id)
        except ConnectorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"connector_id": connector_id, "events": connector.read_events(limit=limit)}

    @app.post(
        "/connectors/{connector_id}/reconnect",
        response_model=ReconnectResponse,
    )
    async def reconnect_connector(connector_id: str) -> dict[str, Any]:
        try:
            connector = manager.get(connector_id)
        except ConnectorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await connector.request_reconnect()
        return {"connector": connector.info()}

    @app.delete("/connectors/{connector_id}", status_code=204)
    async def delete_connector(connector_id: str) -> Response:
        try:
            await manager.stop(connector_id)
        except ConnectorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)

    return app
