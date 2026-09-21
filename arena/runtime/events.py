"""JSONL event logging for arena brain runs."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLogger:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        base: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path).expanduser() if path else None
        self.base = dict(base or {})
        self._lock = threading.Lock()
        self._handle: Any | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")

    def log(self, kind: str, **payload: Any) -> None:
        if self._handle is None:
            return
        event = {
            "ts": time.time(),
            "utc": utc_now(),
            "kind": kind,
            **self.base,
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            with self._lock:
                try:
                    self._handle.close()
                finally:
                    self._handle = None
