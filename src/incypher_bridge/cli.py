"""CLI entry point for the FastAPI bridge service."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .app import create_app
from .settings import SettingsError, load_settings


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incypher-bridge",
        description="FastAPI + asyncio multi-agent bridge for IN-CYPHER raw-TCP challenges.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="start the bridge HTTP API and connector manager")
    serve.add_argument("--api-host", default="127.0.0.1")
    serve.add_argument("--api-port", type=int, default=8765)
    serve.add_argument("--state-dir", default=".cypher_bridge/bridge")
    serve.add_argument("--env-file", default=".env.local")
    serve.add_argument("--team-key-file")
    serve.add_argument("--allow-insecure-key-file", action="store_true")
    serve.add_argument("--max-handshakes", type=int, default=4)
    serve.add_argument("--log-level", default="info")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.error(f"unknown command {args.command!r}")
    try:
        settings = load_settings(
            state_dir=args.state_dir,
            env_file=args.env_file,
            team_key_file=args.team_key_file,
            allow_insecure_file=args.allow_insecure_key_file,
            max_handshakes=args.max_handshakes,
            api_host=args.api_host,
            api_port=args.api_port,
        )
    except SettingsError as exc:
        _eprint(f"error: {exc}")
        return 2
    app = create_app(settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
