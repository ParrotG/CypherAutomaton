"""Single-challenge orchestration on top of the official CTF agent API."""

from __future__ import annotations

import os
import re
import shutil
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
from .runtime.connection_proxy import RawTCPProxy, parse_raw_connection
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
    agent_index: int = 0,
    agent_count: int = 1,
    proxied: bool = False,
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
    if agent_count > 1:
        lines += [
            f"You are agent {agent_index + 1} of {agent_count} working in parallel on the same challenge.",
            "Other agents have independent work directories. Do not read or modify their files.",
            "Only you will be credited for the result; work independently.",
            "",
        ]
    if filenames:
        lines += ["## Files", *[f"- {name}" for name in filenames], ""]
    if connection:
        lines += ["## Live instance connection info", connection, ""]
        text = connection.lower()
        if proxied:
            lines += [
                "The connection above is a local serialized gateway managed by the harness.",
                "Multiple agents may connect, but only one upstream service session is active at a time.",
                "Do not implement the PoW gate; the gateway handles it.",
                "",
            ]
        elif "pow" in text or "pwn" in str(ch.get("category") or "").lower():
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
        re.compile(r"\bevents\.jsonl\b|\bsummary\.json\b|\bsummary\.txt\b"),
        "reading arena logs or summaries is not allowed",
    ),
    (
        re.compile(r"(?:^|[\s'\"=;|&(])\.\.(?:$|[\s'\"/;|&)])"),
        "path traversal (`..`) outside the agent workspace is not allowed",
    ),
)


def _command_block_reason(cmd: str, cdir: Path | None = None) -> str | None:
    for pattern, reason in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(cmd):
            return reason
    if cdir is not None and "/work" in cmd:
        workspace = str(cdir)
        for token in re.findall(r"/work[^\s'\";|&)]*", cmd):
            if not token.startswith(workspace):
                return "absolute path outside the agent workspace is not allowed"
    return None


def make_run_bash(cdir: Path) -> Callable[[str], str]:
    def run_bash(cmd: str) -> str:
        blocked = _command_block_reason(cmd, cdir)
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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _agents_per_challenge(dynamic: bool) -> int:
    default = 2
    env_name = "AGENTS_PER_CHALLENGE"
    count = _env_int(env_name, default)
    if dynamic:
        count = _env_int("DYNAMIC_AGENTS_PER_CHALLENGE", count)
    return max(1, min(4, count))


def _copy_shared_files(shared_dir: Path, agent_dirs: list[Path], filenames: list[str]) -> None:
    for agent_dir in agent_dirs:
        agent_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = shared_dir / name
            if source.is_file():
                shutil.copy2(source, agent_dir / name)


def _append_agent_summary(
    *,
    shared_path: Path,
    lock: threading.Lock,
    agent_index: int,
    summary_text: str,
) -> None:
    with lock:
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        with shared_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Agent {agent_index + 1}\n{summary_text.strip()}\n")


def _run_agent(
    *,
    client: CTFdClient,
    ch: dict[str, Any],
    cid: int,
    attempt: int,
    agent_index: int,
    agent_count: int,
    agent_dir: Path,
    filenames: list[str],
    connection: str | None,
    proxied: bool,
    previous_summary: str | None,
    max_steps: int,
    brain_factory: Callable[..., Any],
    submit_flag: Callable[[str], dict[str, Any]],
    deadline: float | None,
    stop_event: threading.Event,
    summary_path: Path,
    summary_lock: threading.Lock,
) -> dict[str, Any]:
    events_path = agent_dir / "events.jsonl"
    prompt = build_prompt(
        ch,
        cdir=agent_dir,
        filenames=filenames,
        connection=connection,
        previous_summary=previous_summary,
        agent_index=agent_index,
        agent_count=agent_count,
        proxied=proxied,
    )
    try:
        brain = brain_factory(
            run_bash=make_run_bash(agent_dir),
            submit_flag=submit_flag,
            max_steps=max_steps,
            workspace_dir=str(agent_dir),
            events_path=str(events_path),
            log_context={"attempt": attempt, "challenge_id": cid, "agent": agent_index + 1},
            deadline=deadline,
            stop_event=stop_event,
        )
        result = dict(brain.solve(prompt))
    except Exception as exc:  # noqa: BLE001 - agent failures become attempt data
        result = {"solved": False, "error": f"{type(exc).__name__}: {exc}"}
    result["agent"] = agent_index + 1

    try:
        summary = build_attempt_summary(
            challenge_id=cid,
            cdir=agent_dir,
            attempt=attempt,
            result=result,
            events_path=events_path,
        )
        _append_agent_summary(
            shared_path=summary_path,
            lock=summary_lock,
            agent_index=agent_index,
            summary_text=str(summary.get("summary_text") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - summary must not kill the challenge
        result.setdefault("summary_error", f"{type(exc).__name__}: {exc}")
    return result


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
    shared_dir = cdir / "shared"
    dynamic = str(ch.get("type") or "") == "dynamic_iac"
    connection: str | None = None
    proxy: RawTCPProxy | None = None
    filenames: list[str] = []
    started = time.perf_counter()
    deadline_clock = time.monotonic()
    time_limit = float(os.environ.get("CHALLENGE_TIME_LIMIT_SECONDS", "3600"))
    deadline: float | None = deadline_clock + time_limit if time_limit > 0 else None
    final_result: dict[str, Any] = {"solved": False, "error": "not_started"}
    attempts: list[dict[str, Any]] = []
    attempts_made = 0
    started_at = _utc_now()
    submit_flags = _env_bool("SUBMIT_FLAGS", True)
    renew_stop: threading.Event | None = None
    renew_thread: threading.Thread | None = None
    stop_event = threading.Event()
    solved_event = threading.Event()
    submit_lock = threading.Lock()
    summary_lock = threading.Lock()
    requeued = False
    if max_attempts is None:
        max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    max_attempts = max(1, int(max_attempts))
    agent_count = _agents_per_challenge(dynamic)

    def submit_flag(flag: str) -> dict[str, Any]:
        if solved_event.is_set():
            return {"status": "already_solved", "duplicate": True}
        with submit_lock:
            if solved_event.is_set():
                return {"status": "already_solved", "duplicate": True}
            if submit_flags:
                verdict = client.submit(cid, flag)
            else:
                verdict = {"status": "correct", "submission_disabled": True}
            if str((verdict or {}).get("status", "")).lower() in ("correct", "already_solved"):
                solved_event.set()
                stop_event.set()
            return verdict

    try:
        if prepared_filenames is None:
            filenames = prepare_files(client, ch, shared_dir)
        else:
            shared_dir.mkdir(parents=True, exist_ok=True)
            filenames = list(prepared_filenames)

        if dynamic:
            connection = boot_dynamic(client, cid)
            if connection:
                parsed = parse_raw_connection(connection)
                if parsed and agent_count > 1:
                    try:
                        host, port, team_key, pow_required = parsed
                        proxy = RawTCPProxy(
                            host,
                            port,
                            team_key=team_key,
                            pow_required=pow_required,
                        )
                        connection = proxy.start()
                    except Exception:
                        proxy = None
                        agent_count = 1
                renew_interval = float(os.environ.get("RENEW_INTERVAL_SECONDS", "60"))
                renew_stop = threading.Event()
                renew_thread = threading.Thread(
                    target=_renew_loop,
                    args=(client, cid, renew_stop, renew_interval),
                    daemon=True,
                    name=f"renew-{cid}",
                )
                renew_thread.start()

        shared_files = [name for name in filenames if (shared_dir / name).is_file()]
        agent_dirs = [cdir / "agents" / f"agent-{index}" for index in range(agent_count)]
        _copy_shared_files(shared_dir, agent_dirs, shared_files)

        for attempt in range(1, max_attempts + 1):
            if deadline is not None and time.monotonic() >= deadline:
                break
            attempts_made += 1
            previous_summary = attempts[-1]["summary_text"] if attempts else None
            shared_attempt_dir = cdir / "attempts" / f"{attempt:03d}"
            shared_summary_path = shared_attempt_dir / "summary.txt"
            if shared_summary_path.exists():
                shared_summary_path.unlink()

            results: list[dict[str, Any] | None] = [None] * agent_count
            threads: list[threading.Thread] = []
            for index in range(agent_count):
                thread = threading.Thread(
                    target=lambda i=index: results.__setitem__(
                        i,
                        _run_agent(
                            client=client,
                            ch=ch,
                            cid=cid,
                            attempt=attempt,
                            agent_index=i,
                            agent_count=agent_count,
                            agent_dir=agent_dirs[i],
                            filenames=filenames,
                            connection=connection,
                            proxied=proxy is not None,
                            previous_summary=previous_summary,
                            max_steps=max_steps,
                            brain_factory=brain_factory,
                            submit_flag=submit_flag,
                            deadline=deadline,
                            stop_event=stop_event,
                            summary_path=shared_summary_path,
                            summary_lock=summary_lock,
                        ),
                    ),
                    daemon=True,
                    name=f"agent-{cid}-{index}",
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()

            agent_results = [dict(item) for item in results if isinstance(item, dict)]
            solved_results = [item for item in agent_results if item.get("solved")]
            if solved_results:
                final_result = solved_results[0]
                break

            timed_out = deadline is not None and time.monotonic() >= deadline
            error = "challenge_timeout" if timed_out else "unsolved"
            final_result = {
                "solved": False,
                "error": error,
                "steps": max((int(item.get("steps") or 0) for item in agent_results), default=0),
            }
            combined_summary = ""
            if shared_summary_path.exists():
                combined_summary = shared_summary_path.read_text(encoding="utf-8", errors="replace")
            attempts.append(
                {
                    "attempt": attempt,
                    "status": error,
                    "summary_text": combined_summary,
                    "agent_results": agent_results,
                }
            )
            if timed_out:
                requeued = True
                break
            if attempt < max_attempts:
                continue
    except Exception as exc:  # noqa: BLE001 - one challenge must not kill the scheduler
        final_result = {"solved": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop_event.set()
        if proxy is not None:
            proxy.stop()
        if renew_stop is not None:
            renew_stop.set()
        if renew_thread is not None:
            renew_thread.join(timeout=2.0)
        if dynamic:
            destroy_dynamic(client, cid)

    if requeued and not final_result.get("solved"):
        final_result["requeue"] = True
    if (
        deadline is not None
        and time.monotonic() >= deadline
        and not final_result.get("solved")
    ):
        final_result["error"] = "challenge_timeout"
        final_result["timeout"] = True
        final_result["requeue"] = True
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
        "agents_per_challenge": agent_count,
        "attempts": attempts,
        **final_result,
    }
