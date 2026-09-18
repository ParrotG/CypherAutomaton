"""Target classification for scheduled tasks.

Supported targets:

- raw TCP service: ``HOST:PORT`` or ``nc HOST PORT``
- HTTP(S) URL: ``http://...`` or ``https://...``
- local file or directory: an existing path
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from incypher_bridge.connector import ConnectorError, parse_target


class TargetKind(str, Enum):
    RAW_TCP = "raw_tcp"
    URL = "url"
    FILES = "files"


@dataclass(frozen=True)
class TargetSpec:
    kind: TargetKind
    raw: str
    normalized: str
    local_path: str | None = None


def classify_target(value: str) -> TargetSpec:
    text = str(value).strip()
    if not text:
        raise ValueError("target must not be empty")
    if re.match(r"^https?://", text, re.IGNORECASE):
        return TargetSpec(kind=TargetKind.URL, raw=text, normalized=text)
    path = Path(text).expanduser()
    if path.exists():
        resolved = path.resolve()
        return TargetSpec(
            kind=TargetKind.FILES,
            raw=text,
            normalized=str(resolved),
            local_path=str(resolved),
        )
    try:
        host, port = parse_target(text)
    except ConnectorError as exc:
        raise ValueError(
            "target must be HOST:PORT, `nc HOST PORT`, an HTTP(S) URL, "
            "or an existing file/directory path"
        ) from exc
    return TargetSpec(
        kind=TargetKind.RAW_TCP,
        raw=text,
        normalized=f"{host}:{port}",
    )
