"""Human-friendly duration parsing and formatting."""

from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"^\s*(?:(?P<days>\d+(?:\.\d+)?)d)?"
    r"\s*(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"\s*(?:(?P<minutes>\d+(?:\.\d+)?)m)?"
    r"\s*(?:(?P<seconds>\d+(?:\.\d+)?)s)?\s*$",
    re.IGNORECASE,
)
_HHMMSS_RE = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d)$")


def parse_duration(value: str | int | float | None, *, default: float = 3600.0) -> float:
    """Parse ``1h``, ``90m``, ``3600``, ``01:00:00`` or ``1h30m``.

    Bare numbers are seconds.  ``None`` or an empty string returns *default*.
    """

    if value is None or value == "":
        return float(default)
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return float(default)
        if _HHMMSS_RE.match(text):
            hours, minutes, seconds = (int(part) for part in text.split(":"))
            return float(hours * 3600 + minutes * 60 + seconds)
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            seconds = float(text)
        else:
            match = _DURATION_RE.match(text)
            if not match:
                raise ValueError(
                    f"invalid duration {value!r}; use e.g. 3600, 90m, 1h, "
                    "1h30m, or 01:00:00"
                )
            if not any(match.group(name) for name in ("days", "hours", "minutes", "seconds")):
                raise ValueError(f"invalid duration {value!r}")
            seconds = 0.0
            for name, scale in (
                ("days", 86400.0),
                ("hours", 3600.0),
                ("minutes", 60.0),
                ("seconds", 1.0),
            ):
                part = match.group(name)
                if part is not None:
                    seconds += float(part) * scale
    if seconds < 0:
        raise ValueError("duration must be non-negative")
    return seconds


def format_duration(seconds: float | int | None) -> str:
    """Return a compact human-readable duration."""

    if seconds is None:
        return "unlimited"
    remaining = max(0, int(round(float(seconds))))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


def format_clock(seconds: float | int | None) -> str:
    """Return ``HH:MM:SS`` for countdowns; unlimited for ``None``."""

    if seconds is None:
        return "--:--:--"
    remaining = max(0, int(round(float(seconds))))
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
