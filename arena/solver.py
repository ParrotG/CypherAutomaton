"""Single-challenge orchestration on top of the official CTF agent API."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .brain import Brain
from .official.ctfd import CTFdClient

DEFAULT_WORK_ROOT = Path("/work")
BASH_TIMEOUT = 120.0


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
                "import sys; sys.path.insert(0, '/opt/agent')",
                "from ctfd import connect_pwn",
                "sock = connect_pwn(host, port, team_key)",
                "```",
                "",
            ]
    lines += ["Solve the challenge, then call submit_flag with the flag."]
    return "\n".join(lines)


def make_run_bash(cdir: Path) -> Callable[[str], str]:
    def run_bash(cmd: str) -> str:
        try:
            proc = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=str(cdir),
                capture_output=True,
                text=True,
                timeout=BASH_TIMEOUT,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return output if output.strip() else f"(no output, exit={proc.returncode})"
        except subprocess.TimeoutExpired:
            return f"(command timed out after {BASH_TIMEOUT:.0f}s)"
        except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
            return f"(execution error: {type(exc).__name__}: {exc})"

    return run_bash


def solve_challenge(
    client: CTFdClient,
    ch: dict[str, Any],
    max_steps: int = 40,
    *,
    brain_factory: Callable[..., Any] = Brain,
    work_root: Path = DEFAULT_WORK_ROOT,
    events_path: Path | None = None,
) -> dict[str, Any]:
    cid = int(ch["id"])
    cdir = Path(work_root) / str(cid)
    dynamic = str(ch.get("type") or "") == "dynamic_iac"
    connection: str | None = None
    filenames: list[str] = []
    started = time.perf_counter()
    result: dict[str, Any]

    try:
        filenames = prepare_files(client, ch, cdir)
        if dynamic:
            connection = boot_dynamic(client, cid)
        prompt = build_prompt(ch, cdir=cdir, filenames=filenames, connection=connection)
        run_bash = make_run_bash(cdir)
        event_file = events_path if events_path is not None else cdir / "events.jsonl"
        brain = brain_factory(
            run_bash=run_bash,
            submit_flag=lambda flag: client.submit(cid, flag),
            max_steps=max_steps,
            workspace_dir=str(cdir),
            events_path=str(event_file),
        )
        result = dict(brain.solve(prompt))
    except Exception as exc:  # noqa: BLE001 - one challenge must not kill the scheduler
        result = {"solved": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if dynamic:
            destroy_dynamic(client, cid)

    seconds = round(time.perf_counter() - started, 1)
    return {
        "id": cid,
        "name": ch.get("name"),
        "category": ch.get("category"),
        "points": ch.get("points"),
        "type": ch.get("type"),
        "had_files": bool(filenames),
        "had_instance": bool(connection),
        "seconds": seconds,
        **result,
    }
