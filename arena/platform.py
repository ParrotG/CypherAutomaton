"""Platform helpers built directly on the official ``ctfd.py`` client."""

from __future__ import annotations

import os
from typing import Any, Iterable

from .official.ctfd import CTFdClient


class PlatformConfigError(RuntimeError):
    """Raised when official CTF_* environment variables are missing."""


def connect_from_env() -> CTFdClient:
    base = (os.environ.get("CTF_BASE") or os.environ.get("CTFD_URL") or "").strip()
    token = (os.environ.get("CTF_TOKEN") or os.environ.get("CTFD_TOKEN") or "").strip()
    if not base:
        raise PlatformConfigError("CTF_BASE or CTFD_URL is required")
    if not token:
        raise PlatformConfigError("CTF_TOKEN or CTFD_TOKEN is required")
    return CTFdClient(base, token)


def is_practice(ch: dict[str, Any]) -> bool:
    return "practice" in str(ch.get("category") or "").lower()


def mode_from_env() -> str:
    mode = os.environ.get("ARENA_MODE", "competition").strip().lower()
    if mode not in ("practice", "competition"):
        raise PlatformConfigError("ARENA_MODE must be practice or competition")
    return mode


def select_targets(
    challenges: Iterable[dict[str, Any]],
    *,
    mode: str = "competition",
    solved_ids: Iterable[int] | None = None,
    include_solved: bool = False,
    only_ids: Iterable[int] | None = None,
    categories: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter and order challenges by the arena policy.

    Practice mode includes Practice challenges; competition mode excludes them.
    Challenges are sorted by points ascending, then id ascending.
    """

    if mode not in ("practice", "competition"):
        raise PlatformConfigError("mode must be practice or competition")
    solved = {int(cid) for cid in (solved_ids or [])}
    only = {int(cid) for cid in (only_ids or [])}
    cats = {str(c).strip().lower() for c in (categories or []) if str(c).strip()}

    rows: list[dict[str, Any]] = []
    for ch in challenges:
        cid = int(ch["id"])
        ch_mode = "practice" if is_practice(ch) else "competition"
        if mode != ch_mode:
            continue
        if only and cid not in only:
            continue
        if cats and str(ch.get("category") or "").lower() not in cats:
            continue
        if not include_solved and cid in solved:
            continue
        rows.append(dict(ch))
    rows.sort(key=lambda item: (int(item.get("points") or 0), int(item["id"])))
    return rows

