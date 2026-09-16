"""Filesystem layout for challenge and run sessions.

A *challenge* represents one problem, for example ``overflow-ward``.  A *run*
represents one connection instance and solving process for that challenge.
This makes multiple deploys and repeated attempts easy to inspect.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_component(value: str, *, fallback: str = "unnamed") -> str:
    """Make a user-supplied challenge/run id safe as one path component."""

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip())
    cleaned = cleaned.strip(".-")
    return cleaned[:64] or fallback


def default_challenge_id(host: str, port: int) -> str:
    return safe_component(f"challenge-{host}-{port}", fallback="challenge")


def generate_run_id(prefix: str = "run") -> str:
    return safe_component(f"{prefix}-{utc_stamp()}-{uuid.uuid4().hex[:6]}", fallback="run")


@dataclass(frozen=True)
class RunPaths:
    """Paths belonging to one challenge/run pair."""

    state_dir: Path
    challenge_id: str
    run_id: str
    root: Path
    control_socket: Path

    @classmethod
    def create(
        cls,
        state_dir: Path,
        *,
        challenge_id: str,
        run_id: str,
    ) -> "RunPaths":
        state_dir = Path(state_dir).expanduser().resolve()
        challenge_id = safe_component(challenge_id, fallback="challenge")
        run_id = safe_component(run_id, fallback=generate_run_id())

        root = state_dir / "challenges" / challenge_id / "runs" / run_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass

        # AF_UNIX paths are limited to roughly 107 bytes.  Keep the control
        # socket at the short state-directory level and record its exact path
        # in run.json.
        control_socket = state_dir / f"c-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock"
        return cls(
            state_dir=state_dir,
            challenge_id=challenge_id,
            run_id=run_id,
            root=root,
            control_socket=control_socket,
        )

    @property
    def challenge_dir(self) -> Path:
        return self.state_dir / "challenges" / self.challenge_id

    @property
    def challenge_file(self) -> Path:
        return self.challenge_dir / "challenge.json"

    @property
    def challenge_md(self) -> Path:
        return self.challenge_dir / "challenge.md"

    @property
    def runs_dir(self) -> Path:
        return self.challenge_dir / "runs"

    @property
    def run_file(self) -> Path:
        return self.root / "run.json"

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def transcript_file(self) -> Path:
        return self.root / "transcript.log"

    @property
    def challenge_to_agent_file(self) -> Path:
        return self.root / "challenge_to_agent.bin"

    @property
    def agent_to_challenge_file(self) -> Path:
        return self.root / "agent_to_challenge.bin"

    @property
    def session_id(self) -> str:
        return f"{self.challenge_id}/{self.run_id}"


def run_path_for(state_dir: Path, challenge_id: str, run_id: str) -> RunPaths:
    """Return paths for an existing run by id, without requiring one to exist."""

    state_dir = Path(state_dir).expanduser().resolve()
    challenge_id = safe_component(challenge_id, fallback="challenge")
    run_id = safe_component(run_id, fallback="run")
    root = state_dir / "challenges" / challenge_id / "runs" / run_id
    # Existing metadata normally contains the exact control socket path.  The
    # control socket is not needed for reading a stopped run.
    control_socket = state_dir / f"unknown-{os.getpid()}.sock"
    return RunPaths(
        state_dir=state_dir,
        challenge_id=challenge_id,
        run_id=run_id,
        root=root,
        control_socket=control_socket,
    )


def list_challenge_ids(state_dir: Path) -> list[str]:
    challenges_dir = Path(state_dir).expanduser().resolve() / "challenges"
    if not challenges_dir.exists():
        return []
    return sorted(path.name for path in challenges_dir.iterdir() if path.is_dir())


def list_run_paths(state_dir: Path, challenge_id: str | None = None) -> list[RunPaths]:
    state_dir = Path(state_dir).expanduser().resolve()
    if challenge_id:
        challenge_ids = [safe_component(challenge_id, fallback="challenge")]
    else:
        challenge_ids = list_challenge_ids(state_dir)

    runs: list[RunPaths] = []
    for cid in challenge_ids:
        runs_dir = state_dir / "challenges" / cid / "runs"
        if not runs_dir.exists():
            continue
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            runs.append(run_path_for(state_dir, cid, run_dir.name))
    return runs


def newest_run_path(state_dir: Path, challenge_id: str | None = None) -> RunPaths | None:
    runs = list_run_paths(state_dir, challenge_id)
    if not runs:
        return None
    return max(runs, key=lambda item: _run_mtime(item.root))


def _run_mtime(root: Path) -> float:
    try:
        return root.stat().st_mtime
    except OSError:
        return 0.0


def touch_mtime(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_run_dir(
    state_dir: Path,
    *,
    challenge_id: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Resolve a run directory by explicit ids or the active/latest run."""

    state_dir = Path(state_dir).expanduser().resolve()
    if challenge_id:
        cid = safe_component(challenge_id, fallback="challenge")
        runs_dir = state_dir / "challenges" / cid / "runs"
        if run_id:
            run_dir = runs_dir / safe_component(run_id, fallback="run")
            if not run_dir.exists():
                raise FileNotFoundError(
                    f"run not found: challenge={cid!r} run={run_id!r} ({run_dir})"
                )
            return run_dir
        newest = newest_run_path(state_dir, cid)
        if newest is None:
            raise FileNotFoundError(f"no runs found for challenge {cid!r}")
        return newest.root

    active = _read_json(state_dir / "active.json")
    latest = active.get("latest") if isinstance(active.get("latest"), dict) else {}
    candidate = latest.get("run_dir")
    if candidate and Path(candidate).exists():
        return Path(candidate)
    newest = newest_run_path(state_dir)
    if newest is None:
        raise FileNotFoundError(
            f"no bridge runs found under {state_dir}; start one with `serve`"
        )
    return newest.root


def human_age(start_epoch: float | None) -> str:
    if start_epoch is None:
        return "-"
    seconds = max(0, int(time.time() - float(start_epoch)))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, secs = divmod(seconds, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"
