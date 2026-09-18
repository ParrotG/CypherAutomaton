"""CLI for the minimal autonomous CTF agent unit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_ENV_FILE,
    DEFAULT_FLAG_PATTERN,
    DEFAULT_MODEL,
    DEFAULT_STATE_DIR,
    AgentConfigError,
    build_agent_config,
    load_env_values,
    validate_api_key,
)
from .loop import AgentLoop, read_agent_state
from .model import ModelClient, ModelError


def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _print_startup(config) -> None:
    print("=" * 78)
    print("IN-CYPHER minimal agent unit")
    print("=" * 78)
    print(f"Challenge: {config.challenge_id}")
    print(f"Run:       {config.run_id}")
    print(f"Model:     {config.model}")
    print(f"Base URL:  {config.base_url}")
    print(f"Agent:     {config.agent_endpoint or '-'}")
    print(f"Workspace: {config.workspace_dir}")
    print(f"Run dir:   {config.run_dir}")
    context_limit = config.context_window_tokens - config.context_reserve_tokens
    print(
        "Limits: "
        f"max_seconds={config.max_seconds:g}, "
        f"context_window_tokens={config.context_window_tokens}, "
        f"context_reserve_tokens={config.context_reserve_tokens}, "
        f"context_limit_tokens={context_limit}"
    )
    print("=" * 78)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = build_agent_config(
            state_dir=args.state_dir,
            challenge_id=args.challenge_id,
            run_id=args.run_id,
            run_dir=args.run_dir,
            workspace_dir=args.workspace,
            env_file=args.env_file,
            allow_insecure_file=args.allow_insecure_key_file,
            model=args.model,
            base_url=args.base_url,
            thinking=args.thinking,
            temperature=args.temperature,
            max_seconds=args.max_seconds,
            context_window_tokens=args.context_window_tokens,
            context_reserve_tokens=args.context_reserve_tokens,
            bash_timeout=args.bash_timeout,
            max_tool_output=args.max_tool_output,
            flag_pattern=args.flag_pattern,
            verbose=args.verbose,
        )
    except (AgentConfigError, FileNotFoundError) as exc:
        _eprint(f"error: {exc}")
        return 2

    if not args.quiet:
        _print_startup(config)

    loop = AgentLoop(config)
    result = loop.run()
    summary = {
        "status": result.status,
        "flag": result.flag,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "run_dir": str(config.run_dir),
        "agent_state": str(config.run_dir / "agent" / "state.json"),
        "agent_events": str(config.run_dir / "agent" / "events.jsonl"),
    }
    if result.status == "SUCCESS":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _eprint(json.dumps(summary, ensure_ascii=False, indent=2))
    return result.exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        config = build_agent_config(
            state_dir=args.state_dir,
            challenge_id=args.challenge_id,
            run_id=args.run_id,
            run_dir=args.run_dir,
            workspace_dir=args.workspace,
            env_file=args.env_file,
            allow_insecure_file=args.allow_insecure_key_file,
            max_seconds=args.max_seconds,
            context_window_tokens=args.context_window_tokens,
            context_reserve_tokens=args.context_reserve_tokens,
            verbose=args.verbose,
            require_api_key=False,
        )
    except (AgentConfigError, FileNotFoundError) as exc:
        _eprint(f"error: {exc}")
        return 2

    print(f"challenge_id: {config.challenge_id}")
    print(f"run_id:       {config.run_id}")
    print(f"run_dir:      {config.run_dir}")
    print(f"workspace:    {config.workspace_dir}")
    print(f"agent:        {config.agent_endpoint or '-'}")
    print(f"model:        {config.model}")
    print(f"base_url:     {config.base_url}")
    print(f"api_key:      {'set' if config.api_key else 'MISSING'}")
    print(f"thinking:     {config.thinking or '(provider default)'}")
    print(f"flag_pattern: {config.flag_pattern}")
    if config.agent_endpoint:
        host, _, port_text = config.agent_endpoint.replace("tcp://", "").partition(":")
        print(f"endpoint_host: {host}")
        print(f"endpoint_port: {port_text}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """Make one real model call to validate key/base URL/model/tool calling."""

    try:
        env_values = load_env_values(
            args.env_file,
            allow_insecure_file=args.allow_insecure_key_file,
            required=True,
        )
        api_key = validate_api_key(env_values.get("DEEPSEEK_API_KEY", ""))
    except AgentConfigError as exc:
        _eprint(f"error: {exc}")
        return 2
    model = args.model or DEFAULT_MODEL
    base_url = (
        args.base_url
        or env_values.get("DEEPSEEK_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    client = ModelClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        thinking=args.thinking,
        temperature=None,
        max_retries=1,
        timeout=60.0,
    )
    messages = [
        {"role": "system", "content": "You are a connectivity smoke test. Reply with exactly: OK"},
        {"role": "user", "content": "Reply with OK."},
    ]
    try:
        reply = client.chat(messages, tools=[])
    except ModelError as exc:
        _eprint(f"error: live model smoke failed: {exc}")
        return 1
    content = str(reply.message.get("content") or "").strip()
    print(f"model smoke OK: model={model} base_url={base_url}")
    print(f"reply={content[:200]!r}")
    if reply.total_tokens:
        print(
            f"tokens: prompt={reply.prompt_tokens} completion={reply.completion_tokens} "
            f"total={reply.total_tokens}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    run_dir = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else None
    )
    if run_dir is None:
        from incypher_bridge.paths import resolve_run_dir

        run_dir = resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
    state = read_agent_state(run_dir)
    if args.json:
        print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
        return 0
    if not state:
        print(f"(no agent state at {run_dir / 'agent' / 'state.json'})")
        return 0
    for key in (
        "status",
        "flag",
        "stop_reason",
        "model",
        "model_calls",
        "tool_calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "started_at",
        "updated_at",
        "last_error",
    ):
        if key in state:
            print(f"{key:>18}: {state.get(key)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incypher-agent",
        description=(
            "Minimal DeepSeek-Harness-style CTF agent unit: a tiny model-tool "
            "loop with bash + str_replace_editor + report_flag."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the agent unit")
    run.add_argument("--challenge-id")
    run.add_argument("--run-id")
    run.add_argument("--run-dir")
    run.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    run.add_argument("--workspace")
    run.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    run.add_argument("--allow-insecure-key-file", action="store_true")
    run.add_argument("--model", default=None)
    run.add_argument("--base-url", default=None)
    run.add_argument("--thinking", default=None, help="enabled/disabled/或留空")
    run.add_argument("--temperature", type=float, default=None)
    run.add_argument("--max-seconds", type=float, default=3600.0)
    run.add_argument("--context-window-tokens", type=int, default=1_000_000)
    run.add_argument("--context-reserve-tokens", type=int, default=8_000)
    run.add_argument("--bash-timeout", type=float, default=60.0)
    run.add_argument("--max-tool-output", type=int, default=20_000)
    run.add_argument("--flag-pattern", default=DEFAULT_FLAG_PATTERN)
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--quiet", action="store_true")

    doctor = subparsers.add_parser("doctor", help="check agent configuration without calling the model")
    doctor.add_argument("--challenge-id")
    doctor.add_argument("--run-id")
    doctor.add_argument("--run-dir")
    doctor.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    doctor.add_argument("--workspace")
    doctor.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    doctor.add_argument("--allow-insecure-key-file", action="store_true")
    doctor.add_argument("--max-seconds", type=float, default=3600.0)
    doctor.add_argument("--context-window-tokens", type=int, default=1_000_000)
    doctor.add_argument("--context-reserve-tokens", type=int, default=8_000)
    doctor.add_argument("--verbose", action="store_true")

    smoke = subparsers.add_parser(
        "smoke",
        help="make one real model call to validate API key/model connectivity",
    )
    smoke.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    smoke.add_argument("--allow-insecure-key-file", action="store_true")
    smoke.add_argument("--model", default=None)
    smoke.add_argument("--base-url", default=None)
    smoke.add_argument("--thinking", default=None)

    show = subparsers.add_parser("show", help="show agent state for a run")
    show.add_argument("--challenge-id")
    show.add_argument("--run-id")
    show.add_argument("--run-dir")
    show.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    show.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "smoke":
        return cmd_smoke(args)
    if args.command == "show":
        return cmd_show(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
