"""Durable state, event log, and heartbeat for one agent unit."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incypher_bridge.paths import utc_now
from incypher_bridge.transcript import preview_bytes, write_json_atomic


@dataclass
class AgentResult:
    status: str
    flag: str | None = None
    reason: str | None = None
    exit_code: int = 0


@dataclass
class AgentState:
    status: str = "starting"
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    model: str | None = None
    challenge_id: str | None = None
    run_id: str | None = None
    agent_endpoint: str | None = None
    workspace: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    last_error: str | None = None
    flag: str | None = None
    stop_reason: str | None = None
    exit_code: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utc_now()


class AgentStore:
    """Filesystem persistence rooted at ``run_dir/agent``."""

    def __init__(self, run_dir: Path, *, verbose: bool = False) -> None:
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "agent"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.state_file = self.root / "state.json"
        self.events_file = self.root / "events.jsonl"
        self.log_file = self.root / "agent.log"
        self.heartbeat_file = self.root / "heartbeat"
        self.escalation_file = self.root / "escalation.json"
        self.flag_file = self.root / "flag.json"
        self.candidate_file = self.root / "candidate.json"
        self.verification_file = self.root / "verification.json"
        self._lock = threading.RLock()
        self._seq = 0
        self.state = AgentState()
        self.verbose = verbose
        for path in (self.state_file, self.events_file, self.log_file, self.heartbeat_file):
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    def set_state(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)
                else:
                    self.state.extra[key] = value
            self.state.touch()
            write_json_atomic(self.state_file, self.state.__dict__)

    def heartbeat(self, note: str = "") -> None:
        self.heartbeat_file.write_text(
            f"{utc_now()} {note}\n", encoding="utf-8"
        )
        try:
            os.chmod(self.heartbeat_file, 0o600)
        except OSError:
            pass

    def event(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": time.time(),
                "utc": utc_now(),
                "kind": kind,
                "payload": payload or {},
            }
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            try:
                os.chmod(self.events_file, 0o600)
            except OSError:
                pass
            if self.verbose:
                summary = payload if payload else {}
                text = json.dumps(summary, ensure_ascii=False, default=str)
                if len(text) > 500:
                    text = text[:500] + "..."
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(f"{event['utc']} [{kind}] {text}\n")
            return event

    def write_escalation(self, payload: dict[str, Any]) -> None:
        write_json_atomic(self.escalation_file, payload)

    def write_flag(self, flag: str, evidence: str = "") -> None:
        write_json_atomic(
            self.flag_file,
            {
                "flag": flag,
                "evidence": evidence,
                "recorded_at": utc_now(),
            },
        )

    def write_candidate(self, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body.setdefault("recorded_at", utc_now())
        write_json_atomic(self.candidate_file, body)

    def consume_verification(self) -> dict[str, Any] | None:
        if not self.verification_file.exists():
            return None
        try:
            payload = json.loads(self.verification_file.read_text(encoding="utf-8"))
        except Exception:
            payload = {"success": False, "feedback": "invalid verification JSON"}
        try:
            self.verification_file.unlink(missing_ok=True)
        except OSError:
            pass
        return payload if isinstance(payload, dict) else None

    def close(self) -> None:
        self.state.touch()
        write_json_atomic(self.state_file, self.state.__dict__)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(text, truncated)``."""

    if limit <= 0 or len(text) <= limit:
        return text, False
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return text[:head] + "\n...[TRUNCATED]...\n" + text[-tail:], True


def write_tool_artifact(
    agent_root: Path,
    *,
    tool_call_id: str,
    direction: str,
    data: bytes,
) -> Path:
    artifacts = agent_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in tool_call_id)[:64]
    path = artifacts / f"{safe_id}.{direction}.bin"
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def brief_bytes(data: bytes, limit: int = 500) -> str:
    return preview_bytes(data, limit=limit)
