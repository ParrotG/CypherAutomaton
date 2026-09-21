"""Deterministic attempt summaries for retry handoff."""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_recent_events(path: Path, *, attempt: int | None, limit: int = 120) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    recent: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
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
                if attempt is not None and int(event.get("attempt", -1)) != int(attempt):
                    continue
                recent.append(event)
    except OSError:
        return []
    return list(recent)


def build_attempt_summary(
    *,
    challenge_id: int,
    cdir: Path,
    attempt: int,
    result: dict[str, Any],
    events_path: Path,
) -> dict[str, Any]:
    events = _load_recent_events(events_path, attempt=attempt)
    tool_lines: list[str] = []
    last_assistant = ""
    last_tool_result = ""
    for event in events:
        kind = event.get("kind")
        if kind == "model_reply":
            content = event.get("content")
            if content:
                last_assistant = str(content)[:800]
        elif kind == "assistant_plain":
            content = event.get("content")
            if content:
                last_assistant = str(content)[:800]
        elif kind == "tool_call":
            tool_lines.append(
                f"- call {event.get('name')}: {str(event.get('arguments'))[:500]}"
            )
        elif kind == "tool_result":
            last_tool_result = str(event.get("content") or "")[:1200]
    files = sorted(path.name for path in cdir.iterdir() if path.is_file())
    summary_lines = [
        f"Previous attempt: {attempt}",
        f"Status: {result.get('error') or result.get('final') or 'unsolved'}",
        f"Steps: {result.get('steps')}",
        f"Seconds: {result.get('seconds')}",
    ]
    if result.get("context_limit_tokens"):
        summary_lines.append(f"Context limit: {result.get('context_limit_tokens')}")
    if last_assistant:
        summary_lines += ["", "Last assistant text:", last_assistant]
    if tool_lines:
        summary_lines += ["", "Recent tool calls:", *tool_lines[-10:]]
    if last_tool_result:
        summary_lines += ["", "Last tool result excerpt:", last_tool_result]
    if files:
        summary_lines += ["", "Work directory files:", *[f"- {name}" for name in files[:30]]]
    summary_text = "\n".join(summary_lines)
    payload = {
        "attempt": attempt,
        "challenge_id": challenge_id,
        "status": result.get("error") or ("solved" if result.get("solved") else "unsolved"),
        "summary_text": summary_text,
        "result": result,
    }
    _write_json(cdir / "attempts" / f"{attempt:03d}" / "summary.json", payload)
    return payload
