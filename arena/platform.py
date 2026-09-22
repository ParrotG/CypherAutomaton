"""Platform helpers built directly on the official ``ctfd.py`` client."""

from __future__ import annotations

import os
import random
import time
from typing import Any, Iterable
from urllib.error import HTTPError

from .official.ctfd import CTFdClient


class PlatformConfigError(RuntimeError):
    """Raised when official CTF_* environment variables are missing."""


class RetryingCTFdClient(CTFdClient):
    """Official client with bounded retries for transient platform failures.

    The official snapshot under ``arena/official`` remains untouched.  This
    wrapper only retries transport errors, HTTP 429, and HTTP 5xx; 4xx errors
    are returned immediately because they usually indicate auth/request bugs.
    """

    def __init__(
        self,
        base: str,
        token: str,
        *,
        max_attempts: int = 3,
        retry_base: float = 0.5,
        retry_max_wait: float = 5.0,
    ) -> None:
        super().__init__(base, token)
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base = max(0.05, float(retry_base))
        self.retry_max_wait = max(self.retry_base, float(retry_max_wait))

    @staticmethod
    def _env_token() -> str:
        return (
            os.environ.get("CTF_TOKEN")
            or os.environ.get("CTFD_TOKEN")
            or os.environ.get("CTF_SESSION")
            or ""
        ).strip()

    def _refresh_token(self) -> None:
        """Prefer the fresh token injected for this run.

        The official runner mints a new CTFd token at the start of each run.
        Re-reading the environment before every platform call ensures that a
        token injected after process start (or a newer value replacing an
        older local one) is actually used for submissions.
        """
        token = self._env_token()
        if token:
            self.token = token

    def _retry_wait(self, attempt: int) -> float:
        base = min(self.retry_max_wait, self.retry_base * (2 ** max(0, attempt - 1)))
        return base * random.uniform(0.5, 1.5)

    def _call(self, method, path, body=None):
        last_code = -1
        last_payload: dict[str, Any] = {"ok": False, "error": "no attempt", "data": None}
        for attempt in range(1, self.max_attempts + 1):
            self._refresh_token()
            code, payload = super()._call(method, path, body)
            last_code, last_payload = code, payload
            retryable = code == -1 or code == 429 or code >= 500
            if not retryable or attempt >= self.max_attempts:
                return code, payload
            time.sleep(self._retry_wait(attempt))
        return last_code, last_payload

    def download(self, url: str, dest: str):
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._refresh_token()
            try:
                return super().download(url, dest)
            except HTTPError as exc:
                # Do not retry ordinary client errors (404/403/...).  429 is
                # treated as transient by the platform.
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise
                last_error = exc
            except Exception as exc:  # network/DNS/timeout etc.
                last_error = exc
            if attempt >= self.max_attempts:
                break
            time.sleep(self._retry_wait(attempt))
        if last_error is not None:
            raise last_error
        return None


def connect_from_env() -> CTFdClient:
    base = (os.environ.get("CTF_BASE") or os.environ.get("CTFD_URL") or "").strip()
    token = (
        os.environ.get("CTF_TOKEN")
        or os.environ.get("CTFD_TOKEN")
        or os.environ.get("CTF_SESSION")
        or ""
    ).strip()
    if not base:
        raise PlatformConfigError("CTF_BASE or CTFD_URL is required")
    if not token:
        raise PlatformConfigError("CTF_TOKEN, CTFD_TOKEN or CTF_SESSION is required")
    return RetryingCTFdClient(base, token)


def is_practice(ch: dict[str, Any]) -> bool:
    return "practice" in str(ch.get("category") or "").lower()


def mode_from_env() -> str:
    """Return the requested target mode.

    ``auto`` is the default so the same image can run Day 1 (practice set only)
    and Day 2 (competition set present) without runtime arguments.
    """
    mode = os.environ.get("ARENA_MODE", "auto").strip().lower()
    if mode not in ("auto", "practice", "competition"):
        raise PlatformConfigError("ARENA_MODE must be auto, practice or competition")
    return mode


def resolve_mode(
    challenges: Iterable[dict[str, Any]],
    requested: str | None = None,
) -> str:
    """Resolve ``auto`` to practice/competition from the live challenge list.

    Competition challenges win when both sets are visible.  If only practice
    challenges exist, as on Day 1, fall back to practice.
    """
    mode = requested or mode_from_env()
    if mode in ("practice", "competition"):
        return mode
    if mode != "auto":
        raise PlatformConfigError(f"unsupported mode: {mode}")

    rows = list(challenges)
    if any(not is_practice(ch) for ch in rows):
        return "competition"
    if rows:
        return "practice"
    # No challenges at all: preserve the old competition default so an empty
    # run still exits cleanly and writes results.json.
    return "competition"


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
