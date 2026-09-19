"""Configuration for the simple task scheduler."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def safe_component(value: str, *, fallback: str = "task") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip(".-")
    return cleaned[:64] or fallback


@dataclass(frozen=True)
class SchedulerConfig:
    task_id: str
    target: str
    state_dir: Path
    max_concurrent_workers: int = 2
    max_total_workers: int = 8

    description_file: str | None = None
    description_text: str | None = None
    challenge_name: str | None = None
    category: str | None = None

    worker_max_seconds: float = 3600.0
    worker_context_window_tokens: int = 1_000_000
    worker_context_reserve_tokens: int = 8_000
    worker_bash_timeout: float = 60.0
    worker_max_tool_output: int = 20_000

    env_file: str = ".env.local"
    team_key_file: str | None = None
    allow_insecure_key_file: bool = False
    max_handshakes: int = 4

    bridge_url: str | None = None
    bridge_port: int = 0
    bridge_host: str = "127.0.0.1"

    model: str | None = None
    base_url: str | None = None
    thinking: str | None = None
    temperature: float | None = None

    sandbox_backend: str = "bwrap"
    sandbox_tool_root: str | None = None
    sandbox_bwrap: str = "bwrap"
    sandbox_network: bool = True

    worker_command: tuple[str, ...] = (
        "python",
        "-m",
        "incypher_agent",
        "run",
    )

    @property
    def task_root(self) -> Path:
        return self.state_dir / "tasks" / safe_component(self.task_id)

    @property
    def attempts_root(self) -> Path:
        return self.task_root / "attempts"

    def validate(self) -> None:
        if self.max_concurrent_workers < 1:
            raise ValueError("max_concurrent_workers must be positive")
        if self.max_total_workers < 1:
            raise ValueError("max_total_workers must be positive")
        if self.max_concurrent_workers > self.max_total_workers:
            raise ValueError(
                "max_concurrent_workers cannot exceed max_total_workers"
            )
        if not self.target.strip():
            raise ValueError("target is required")
        from .targets import classify_target

        classify_target(self.target)
        if not self.worker_command:
            raise ValueError("worker_command cannot be empty")
        if self.sandbox_backend not in ("bwrap", "local"):
            raise ValueError("sandbox_backend must be one of: bwrap, local")


def generate_attempt_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return safe_component(f"attempt-{stamp}-{uuid.uuid4().hex[:6]}", fallback="attempt")


def parse_worker_command(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker command must be a JSON array: {exc}") from exc
    if not isinstance(payload, list) or not payload or not all(
        isinstance(item, str) and item for item in payload
    ):
        raise ValueError("worker command must be a non-empty JSON array of strings")
    return tuple(payload)
