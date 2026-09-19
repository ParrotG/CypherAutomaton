#!/usr/bin/env python3
"""Tiny atomic blackboard helper exposed inside worker sandboxes.

The helper does not parse record content.  It only provides:

- ``bb-write``: read stdin and atomically create one free-form record file.
- ``bb-read``: print existing records in name order.

Agents can ignore these commands and use the blackboard directory directly.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _blackboard_root() -> Path:
    return Path(os.environ.get("INCYPHER_BLACKBOARD", "/blackboard"))


def _records_dir() -> Path:
    return _blackboard_root() / "records"


def _safe(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in value.strip()
    )[:64] or "worker"


def _write_record() -> int:
    data = sys.stdin.buffer.read()
    if not data.strip():
        print("bb-write: empty stdin; nothing recorded", file=sys.stderr)
        return 1
    records = _records_dir()
    records.mkdir(parents=True, exist_ok=True)
    worker = _safe(os.environ.get("INCYPHER_WORKER_ID", "worker"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_name = f"agent-{worker}-{stamp}-{uuid.uuid4().hex[:8]}.md"
    final_path = records / final_name
    temp_path = records / f".{final_name}.tmp"
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    print(str(final_path))
    return 0


def _read_records(argv: list[str]) -> int:
    limit: int | None = None
    if argv:
        try:
            limit = max(1, int(argv[0]))
        except ValueError:
            print("bb-read: optional argument must be a record limit", file=sys.stderr)
            return 2
    records = _records_dir()
    if not records.exists():
        return 0
    files = sorted(
        path
        for path in records.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    if limit is not None:
        files = files[-limit:]
    for path in files:
        print(f"===== {path.name} =====")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[bb-read] cannot read {path}: {exc}")
            continue
        sys.stdout.write(content)
        if not content.endswith("\n"):
            print()
    return 0


def main() -> int:
    name = Path(sys.argv[0]).name
    if name.startswith("bb-read"):
        return _read_records(sys.argv[1:])
    if name.startswith("bb-write"):
        return _write_record()
    if len(sys.argv) < 2 or sys.argv[1] not in ("read", "write"):
        print("usage: blackboard_cli.py read [limit] | write", file=sys.stderr)
        return 2
    if sys.argv[1] == "read":
        return _read_records(sys.argv[2:])
    return _write_record()


if __name__ == "__main__":
    raise SystemExit(main())
