"""Human-readable event and raw-stream logging for a bridge session."""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .duration import format_clock
from .paths import RunPaths, utc_now


def preview_bytes(data: bytes, limit: int = 160) -> str:
    """Return a printable, human-readable preview of a byte string."""

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


def hex_preview(data: bytes, limit: int = 64) -> str:
    shown = data[:limit]
    text = " ".join(f"{byte:02x}" for byte in shown)
    if len(data) > limit:
        text += " ..."
    return text


class EventLogger:
    """Thread-safe event log with full raw byte streams.

    ``events.jsonl`` is machine-readable and contains a short preview plus the
    offset into the relevant raw stream.  ``transcript.log`` is immediately
    readable with ``tail -f``.
    """

    def __init__(self, paths: RunPaths, *, max_preview: int = 160) -> None:
        self.paths = paths
        self.max_preview = max_preview
        self._lock = threading.RLock()
        self._seq = 0
        self._events_handle = paths.events_file.open("a", encoding="utf-8")
        self._transcript_handle = paths.transcript_file.open("a", encoding="utf-8")
        self._upstream_handle = paths.challenge_to_agent_file.open("ab")
        self._downstream_handle = paths.agent_to_challenge_file.open("ab")
        self._upstream_offset = 0
        self._downstream_offset = 0
        for file_path in (
            paths.events_file,
            paths.transcript_file,
            paths.challenge_to_agent_file,
            paths.agent_to_challenge_file,
        ):
            try:
                os.chmod(file_path, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            for handle in (
                self._events_handle,
                self._transcript_handle,
                self._upstream_handle,
                self._downstream_handle,
            ):
                try:
                    handle.close()
                except Exception:
                    pass

    def record(self, direction: str, data: bytes, source: str = "") -> dict[str, Any]:
        """Record one chunk and return its JSON event."""

        if not data and direction != "SYS":
            return {}
        with self._lock:
            self._seq += 1
            sequence = self._seq
            raw_file = ""
            offset = 0
            if direction == "C->A":
                raw_file = self.paths.challenge_to_agent_file.name
                offset = self._upstream_offset
                self._upstream_handle.write(data)
                self._upstream_handle.flush()
                self._upstream_offset += len(data)
            elif direction == "A->C":
                raw_file = self.paths.agent_to_challenge_file.name
                offset = self._downstream_offset
                self._downstream_handle.write(data)
                self._downstream_handle.flush()
                self._downstream_offset += len(data)

            event: dict[str, Any] = {
                "seq": sequence,
                "ts": datetime.now(timezone.utc).timestamp(),
                "utc": utc_now(),
                "dir": direction,
                "source": source,
                "len": len(data),
                "preview": preview_bytes(data, self.max_preview),
                "hex": hex_preview(data, 32) if data else "",
                "raw_file": raw_file,
                "offset": offset,
            }
            if direction == "SYS":
                event["message"] = data.decode("utf-8", errors="replace")
                event["preview"] = event["message"]
            self._events_handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events_handle.flush()

            stamp = datetime.fromtimestamp(event["ts"], tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            if direction == "C->A":
                label = "challenge -> agent"
            elif direction == "A->C":
                label = "agent -> challenge"
            else:
                label = "system"
            self._transcript_handle.write(
                f"{stamp} [{label:>18}] {len(data):>7}B  {event['preview']}\n"
            )
            self._transcript_handle.flush()
            return event

    def system(self, message: str, *, level: str = "INFO") -> dict[str, Any]:
        return self.record("SYS", f"[{level}] {message}".encode("utf-8"), source="bridge")

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "events": self._seq,
                "challenge_to_agent_bytes": self._upstream_offset,
                "agent_to_challenge_bytes": self._downstream_offset,
            }

    @staticmethod
    def read_events(path: Path, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Read events from a session JSONL file, filtering by sequence number."""

        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(event.get("seq", 0)) <= since:
                        continue
                    events.append(event)
                    if len(events) >= limit:
                        break
        except OSError:
            return []
        return events


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically and privately where possible."""

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


def format_status_lines(status: dict[str, Any]) -> list[str]:
    """Convert a status dict into a readable multi-line block."""

    remaining = status.get("remaining_seconds")
    lines = [
        f"Challenge: {status.get('challenge_id')}",
        f"Run:       {status.get('run_id')}",
        f"State:     {status.get('state')}",
        f"Target:    {status.get('target')}",
        f"Agent:     {status.get('agent_endpoint') or '-'}"
        f"{'  (waiting for agent)' if not status.get('client_connected') else '  (agent connected)'}",
        f"PoW:       {status.get('pow_summary') or '-'}",
        f"Uptime:    {format_clock(status.get('uptime_seconds'))}",
        f"Remaining: {format_clock(remaining)}"
        f"{'  (expired)' if status.get('expired') else ''}",
        f"Events:    {status.get('events', 0)}",
        f"Traffic:   challenge->agent={status.get('challenge_to_agent_bytes', 0)}B, "
        f"agent->challenge={status.get('agent_to_challenge_bytes', 0)}B",
        f"Challenge dir: {status.get('challenge_dir')}",
        f"Run dir:       {status.get('session_dir')}",
        f"Description:   {status.get('challenge_md') or '-'}"
        f"{'' if status.get('description_present') else '  (not provided)'}",
        f"Control:       {status.get('control_socket')}",
    ]
    error = status.get("last_error")
    if error:
        lines.append(f"Last error: {error}")
    return lines


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
