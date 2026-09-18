"""CLI for the simple task scheduler."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import SchedulerConfig, parse_worker_command
from .scheduler import SimpleScheduler


def _default_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"task-{stamp}-{uuid.uuid4().hex[:6]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incypher-scheduler",
        description="Simple task-blind scheduler for concurrent IN-CYPHER agent workers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="start a task and maintain workers")
    run.add_argument("--task-id", default=None)
    run.add_argument("--target", required=True, help="Raw TCP target: HOST:PORT")
    run.add_argument("--description-file")
    run.add_argument("--description-text")
    run.add_argument("--challenge-name")
    run.add_argument("--category")
    run.add_argument("--state-dir", default=".cypher_bridge/scheduler")
    run.add_argument("--max-concurrent-workers", type=int, default=2)
    run.add_argument("--max-total-workers", type=int, default=8)
    run.add_argument("--worker-max-seconds", type=float, default=3600.0)
    run.add_argument("--worker-context-window-tokens", type=int, default=1_000_000)
    run.add_argument("--worker-context-reserve-tokens", type=int, default=8_000)
    run.add_argument("--worker-bash-timeout", type=float, default=60.0)
    run.add_argument("--worker-max-tool-output", type=int, default=20_000)
    run.add_argument("--env-file", default=".env.local")
    run.add_argument("--team-key-file")
    run.add_argument("--allow-insecure-key-file", action="store_true")
    run.add_argument("--max-handshakes", type=int, default=4)
    run.add_argument("--bridge-url", help="Use an existing bridge service instead of starting one")
    run.add_argument("--bridge-host", default="127.0.0.1")
    run.add_argument("--bridge-port", type=int, default=0)
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--thinking")
    run.add_argument("--temperature", type=float)
    run.add_argument(
        "--worker-command-json",
        help=(
            "Override the worker command prefix as a JSON array. "
            "The scheduler appends --run-dir, --agent-endpoint and worker limits."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        print(json.dumps({"ok": False, "error": f"unknown command {args.command!r}"}))
        return 2
    try:
        worker_command = (
            parse_worker_command(args.worker_command_json)
            if args.worker_command_json
            else (sys.executable, "-m", "incypher_agent", "run")
        )
        config = SchedulerConfig(
            task_id=args.task_id or _default_task_id(),
            target=args.target,
            state_dir=Path(args.state_dir).expanduser().resolve(),
            max_concurrent_workers=args.max_concurrent_workers,
            max_total_workers=args.max_total_workers,
            description_file=args.description_file,
            description_text=args.description_text,
            challenge_name=args.challenge_name,
            category=args.category,
            worker_max_seconds=args.worker_max_seconds,
            worker_context_window_tokens=args.worker_context_window_tokens,
            worker_context_reserve_tokens=args.worker_context_reserve_tokens,
            worker_bash_timeout=args.worker_bash_timeout,
            worker_max_tool_output=args.worker_max_tool_output,
            env_file=args.env_file,
            team_key_file=args.team_key_file,
            allow_insecure_key_file=args.allow_insecure_key_file,
            max_handshakes=args.max_handshakes,
            bridge_url=args.bridge_url,
            bridge_host=args.bridge_host,
            bridge_port=args.bridge_port,
            model=args.model,
            base_url=args.base_url,
            thinking=args.thinking,
            temperature=args.temperature,
            worker_command=worker_command,
        )
        config.validate()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    scheduler = SimpleScheduler(config)
    try:
        code = asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": code == 0,
                "exit_code": code,
                "task_id": config.task_id,
                "state_dir": str(config.task_root),
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
