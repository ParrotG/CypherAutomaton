"""Single-challenge orchestration on top of the official CTF agent API."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .brain import Brain
from .official.ctfd import CTFdClient
from .runtime.summary import build_attempt_summary

DEFAULT_WORK_ROOT = Path("/work")
BASH_TIMEOUT = 120.0


def default_work_root() -> Path:
    """Return a writable work root for local tests or the arena /work mount."""
    explicit = os.environ.get("WORK_ROOT") or os.environ.get("ARENA_WORK_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        DEFAULT_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        if os.access(DEFAULT_WORK_ROOT, os.W_OK):
            return DEFAULT_WORK_ROOT.resolve()
    except OSError:
        pass
    fallback = Path(tempfile.gettempdir()) / "arena-work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _agent_home() -> str:
    """Locate the directory that contains the official ``ctfd.py`` helper."""
    explicit = os.environ.get("AGENT_HOME")
    if explicit:
        return explicit
    if Path("/opt/agent/ctfd.py").is_file():
        return "/opt/agent"
    local = Path(__file__).resolve().parent / "official"
    if (local / "ctfd.py").is_file():
        return str(local)
    return "/opt/agent"


def _renew_loop(client: CTFdClient, cid: int, stop_event: threading.Event, interval: float) -> None:
    """Best-effort dynamic instance renewal while an attempt is running."""
    if interval <= 0:
        return
    while not stop_event.wait(interval):
        try:
            client.renew(cid)
        except Exception:
            # Renewal is best effort.  A failed renew does not abort solving;
            # the next tick will try again.
            pass


def prepare_files(client: CTFdClient, ch: dict[str, Any], cdir: Path) -> list[str]:
    cdir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for url in ch.get("files") or []:
        base = os.path.basename(str(url).split("?")[0]) or "file"
        try:
            client.download(str(url), str(cdir / base))
            names.append(base)
        except Exception as exc:  # report download failure to the agent
            names.append(f"{base} (download failed: {exc})")
    return names


def boot_dynamic(client: CTFdClient, cid: int) -> str | None:
    data = client.boot(cid)
    if data.get("error"):
        data = client.instance_status(cid)
    info = data.get("connectionInfo")
    if info:
        return str(info)
    for _ in range(6):
        time.sleep(5)
        info = client.instance_status(cid).get("connectionInfo")
        if info:
            return str(info)
    return None


def destroy_dynamic(client: CTFdClient, cid: int) -> None:
    try:
        client.destroy(cid)
    except Exception:
        pass


def build_prompt(
    ch: dict[str, Any],
    *,
    cdir: Path,
    filenames: list[str],
    connection: str | None,
    previous_summary: str | None = None,
) -> str:
    lines = [
        f"# Challenge: {ch.get('name')}",
        f"Challenge ID: {ch.get('id')}",
        f"Category: {ch.get('category')}   Points: {ch.get('points')}   Type: {ch.get('type')}",
        f"Work directory: {cdir}",
        "",
        "## Description",
        str(ch.get("description") or "(none)").strip(),
        "",
    ]
    if filenames:
        lines += ["## Files", *[f"- {name}" for name in filenames], ""]
    if connection:
        lines += ["## Live instance connection info", connection, ""]
        text = connection.lower()
        if "pow" in text or "pwn" in str(ch.get("category") or "").lower():
            lines += [
                "This service may be PoW-gated. Do not implement the gate and do not use plain nc.",
                "Use the official helper:",
                "",
                "```python",
                f"import sys; sys.path.insert(0, {_agent_home()!r})",
                "from ctfd import connect_pwn",
                "sock = connect_pwn(host, port, team_key)",
                "```",
                "",
            ]
    if previous_summary:
        lines += [
            "## Previous attempt summary",
            previous_summary.strip(),
            "",
            "Continue from this summary. Do not repeat failed actions if they produced no new information.",
            "",
        ]
    lines += ["Solve the challenge, then call submit_flag with the flag."]
    return "\n".join(lines)


_BLOCKED_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:^|[;&|]\s*)find\s+/(?=\s|$)", re.M),
        "recursive root filesystem scan (`find /`) is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)find\s+/(?:proc|sys|opt|dev)(?=\s|/)", re.M),
        "system directory scan is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)find\s+/work(?=\s|$)", re.M),
        "scanning the shared /work root is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)grep\b[^\n;|&]*\s/(?=\s|$)", re.M),
        "root filesystem grep is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)rg\b[^\n;|&]*\s/(?=\s|$)", re.M),
        "root filesystem ripgrep is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)grep\b[^\n;|&]*\s/work(?=\s|$)", re.M),
        "scanning the shared /work root is not allowed",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)cd\s+/(?=\s|$)", re.M),
        "changing to the filesystem root is not allowed",
    ),
    (
        re.compile(r"\bevents\.jsonl\b|\bsummary\.json\b"),
        "reading arena logs or summaries is not allowed",
    ),
)


def _command_block_reason(cmd: str) -> str | None:
    for pattern, reason in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(cmd):
            return reason
    return None


def make_run_bash(cdir: Path) -> Callable[[str], str]:
    def run_bash(cmd: str) -> str:
        blocked = _command_block_reason(cmd)
        if blocked:
            return f"(blocked by arena bash policy: {blocked})"
        try:
            proc = subprocess.Popen(
                ["bash", "-lc", cmd],
                cwd=str(cdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                output, _ = proc.communicate(timeout=BASH_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Kill the whole process group.  A plain Popen.kill() only
                # terminates bash and can leave grandchildren holding the
                # output pipe open, which used to make run_bash hang forever.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                output, _ = proc.communicate()
                return f"(command timed out after {BASH_TIMEOUT:.0f}s)" + (
                    "\n" + output if output else ""
                )
            return output if output.strip() else f"(no output, exit={proc.returncode})"
        except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
            return f"(execution error: {type(exc).__name__}: {exc})"

    return run_bash


def solve_challenge(
    client: CTFdClient,
    ch: dict[str, Any],
    max_steps: int = 40,
    *,
    max_attempts: int | None = None,
    brain_factory: Callable[..., Any] = Brain,
    work_root: Path | None = None,
    events_path: Path | None = None,
    prepared_filenames: list[str] | None = None,
) -> dict[str, Any]:
    if work_root is None:
        work_root = default_work_root()
    work_root = Path(work_root)
    cid = int(ch["id"])
    cdir = work_root / str(cid)
    dynamic = str(ch.get("type") or "") == "dynamic_iac"
    connection: str | None = None
    filenames: list[str] = []
    started = time.perf_counter()
    final_result: dict[str, Any] = {"solved": False, "error": "not_started"}
    attempts: list[dict[str, Any]] = []
    attempts_made = 0
    started_at = _utc_now()
    submit_flags = _env_bool("SUBMIT_FLAGS", True)
    renew_stop: threading.Event | None = None
    renew_thread: threading.Thread | None = None
    if max_attempts is None:
        max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    max_attempts = max(1, int(max_attempts))

    try:
        if prepared_filenames is None:
            filenames = prepare_files(client, ch, cdir)
        else:
            cdir.mkdir(parents=True, exist_ok=True)
            filenames = list(prepared_filenames)
        if dynamic:
            connection = boot_dynamic(client, cid)
            if connection:
                renew_interval = float(os.environ.get("RENEW_INTERVAL_SECONDS", "60"))
                renew_stop = threading.Event()
                renew_thread = threading.Thread(
                    target=_renew_loop,
                    args=(client, cid, renew_stop, renew_interval),
                    daemon=True,
                    name=f"renew-{cid}",
                )
                renew_thread.start()
        run_bash = make_run_bash(cdir)
        event_file = events_path if events_path is not None else cdir / "events.jsonl"

        def submit_flag(flag: str) -> dict[str, Any]:
            if submit_flags:
                return client.submit(cid, flag)
            return {"status": "correct", "submission_disabled": True}

        for attempt in range(1, max_attempts + 1):
            attempts_made += 1
            previous_summary = attempts[-1]["summary_text"] if attempts else None
            prompt = build_prompt(
                ch,
                cdir=cdir,
                filenames=filenames,
                connection=connection,
                previous_summary=previous_summary,
            )
            brain = brain_factory(
                run_bash=run_bash,
                submit_flag=submit_flag,
                max_steps=max_steps,
                workspace_dir=str(cdir),
                events_path=str(event_file),
                log_context={"attempt": attempt, "challenge_id": cid},
            )
            try:
                result = dict(brain.solve(prompt))
            except Exception as exc:  # noqa: BLE001 - turn crashes into a retryable attempt
                result = {"solved": False, "error": f"{type(exc).__name__}: {exc}"}
            result["attempt"] = attempt
            final_result = result
            if result.get("solved"):
                break
            summary = build_attempt_summary(
                challenge_id=cid,
                cdir=cdir,
                attempt=attempt,
                result=result,
                events_path=event_file,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "status": summary.get("status"),
                    "summary_text": summary.get("summary_text", ""),
                    "result": result,
                }
            )
            if attempt < max_attempts:
                continue
    except Exception as exc:  # noqa: BLE001 - one challenge must not kill the scheduler
        final_result = {"solved": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if renew_stop is not None:
            renew_stop.set()
        if renew_thread is not None:
            renew_thread.join(timeout=2.0)
        if dynamic:
            destroy_dynamic(client, cid)

    seconds = round(time.perf_counter() - started, 1)
    finished_at = _utc_now()
    return {
        "id": cid,
        "name": ch.get("name"),
        "category": ch.get("category"),
        "points": ch.get("points"),
        "type": ch.get("type"),
        "had_files": bool(filenames),
        "had_instance": bool(connection),
        "seconds": seconds,
        "started_at": started_at,
        "finished_at": finished_at,
        "submit_flags": submit_flags,
        "attempt_count": attempts_made or 1,
        "attempts": attempts,
        **final_result,
    }
