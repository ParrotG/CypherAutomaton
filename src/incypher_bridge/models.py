"""Pydantic request/response models for the bridge HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConnectorCreate(BaseModel):
    connector_id: str | None = Field(
        default=None,
        description="Optional stable connector id. Generated when omitted.",
    )
    target: str = Field(description="Raw TCP target: HOST:PORT or `nc HOST PORT`.")
    bind_host: str = Field(default="127.0.0.1", description="Local bind host.")
    bind_port: int = Field(default=0, ge=0, le=65535, description="Local bind port; 0 = auto.")
    auto_reconnect: bool = Field(
        default=True,
        description="Reconnect automatically after upstream loss.",
    )
    reconnect_delay: float = Field(default=1.0, ge=0.0, description="Seconds between reconnect attempts.")
    connect_timeout: float = Field(default=10.0, ge=0.1, description="TCP connect timeout.")
    banner_timeout: float = Field(default=10.0, ge=0.1, description="PoW banner read timeout.")
    pow_timeout: float = Field(default=120.0, ge=0.1, description="PoW solve timeout.")
    max_pending_bytes: int = Field(default=1_048_576, ge=1, description="Bounded downstream buffer.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorInfo(BaseModel):
    connector_id: str
    target: str
    endpoint: str | None = None
    bind_host: str
    bind_port: int | None = None
    state: str
    generation: int
    client_connected: bool = False
    pending_bytes: int = 0
    pow_summary: str | None = None
    last_error: str | None = None
    auto_reconnect: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    directory: str


class ConnectorList(BaseModel):
    connectors: list[ConnectorInfo]


class EventList(BaseModel):
    connector_id: str
    events: list[dict[str, Any]]


class ReconnectResponse(BaseModel):
    connector: ConnectorInfo


class HealthResponse(BaseModel):
    ok: bool
    connectors: int
    service: str = "incypher-bridge-v2"
