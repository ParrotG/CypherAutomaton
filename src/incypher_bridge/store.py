"""Small filesystem store for connector metadata, events and raw streams."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def preview_bytes(data: bytes, limit: int = 160) -> str:
    shown = data[:limit]
    out: list[str] = []
    for byte in shown:
        if byte == 0x0A:
            out.append("\\n")
        elif byte == 0x0D:
            out.append("\\r")
        elif byte == 0x09:
            out.append("\\t")
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    text = "".join(out)
    if len(data) > limit:
        text += f"...(+{len(data) - limit}B)"
    return text


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


class ConnectorStore:
    """Thread-safe event/raw-stream logger for one connector directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.events_file = self.root / "events.jsonl"
        self.meta_file = self.root / "connector.json"
        self.upstream_file = self.root / "challenge_to_agent.bin"
        self.downstream_file = self.root / "agent_to_challenge.bin"
        self._lock = threading.RLock()
        self._seq = 0
        self._upstream_offset = 0
        self._downstream_offset = 0
        self._events_handle = self.events_file.open("a", encoding="utf-8")
        self._upstream_handle = self.upstream_file.open("ab")
        self._downstream_handle = self.downstream_file.open("ab")
        for path in (self.events_file, self.upstream_file, self.downstream_file):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            for handle in (
                self._events_handle,
                self._upstream_handle,
                self._downstream_handle,
            ):
                try:
                    handle.close()
                except OSError:
                    pass

    def write_meta(self, payload: dict[str, Any]) -> None:
        with self._lock:
            write_json_atomic(self.meta_file, payload)

    def record(self, direction: str, data: bytes, source: str = "") -> dict[str, Any]:
        if not data and direction in ("C->A", "A->C"):
            return {}
        with self._lock:
            self._seq += 1
            sequence = self._seq
            raw_file = ""
            offset = 0
            if direction == "C->A":
                raw_file = self.upstream_file.name
                offset = self._upstream_offset
                self._upstream_handle.write(data)
                self._upstream_handle.flush()
                self._upstream_offset += len(data)
            elif direction == "A->C":
                raw_file = self.downstream_file.name
                offset = self._downstream_offset
                self._downstream_handle.write(data)
                self._downstream_handle.flush()
                self._downstream_offset += len(data)

            event: dict[str, Any] = {
                "seq": sequence,
                "ts": time.time(),
                "utc": utc_now(),
                "dir": direction,
                "source": source,
                "len": len(data),
                "preview": preview_bytes(data),
                "raw_file": raw_file,
                "offset": offset,
            }
            if direction == "SYS":
                event["message"] = data.decode("utf-8", errors="replace")
                event["preview"] = event["message"]
            self._events_handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events_handle.flush()
            return event

    def system(self, message: str, *, level: str = "INFO") -> dict[str, Any]:
        return self.record("SYS", f"[{level}] {message}".encode("utf-8"), source="bridge")

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "events": self._seq,
                "challenge_to_agent_bytes": self._upstream_offset,
                "agent_to_challenge_bytes": self._downstream_offset,
            }

    def read_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if not self.events_file.exists():
                return []
            events: list[dict[str, Any]] = []
            try:
                with self.events_file.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        events.append(event)
            except OSError:
                return []
            return events[-max(1, limit):]
