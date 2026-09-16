"""Human-readable CLI for the temporary IN-CYPHER PoW bridge."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .config import TeamKeyError, find_env_file, load_team_key
from .control import ControlError, ControlServer, request as control_request
from .duration import format_clock, format_duration, parse_duration
from .paths import (
    RunPaths,
    default_challenge_id,
    generate_run_id,
    human_age,
    list_run_paths,
    safe_component,
    utc_now,
)
from .pow import DEFAULT_VARIANTS, PowError, PowVariant, solve_pow
from .session import BridgeConfig, BridgeSession
from .transcript import EventLogger, format_status_lines, write_json_atomic

DEFAULT_STATE_DIR = ".cypher_bridge"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def parse_target(text: str) -> tuple[str, int]:
    """Parse ``host:port``, ``nc -v host port`` or ``host port``."""

    value = text.strip()
    if not value:
        raise ValueError("empty target")

    match = re.search(r"\bnc\b(?:\s+-\w+)*\s+(\S+)\s+(\d+)\b", value)
    if match:
        return match.group(1).strip("[]"), int(match.group(2))

    match = re.fullmatch(r"\[([^\]]+)\]\s*:\s*(\d+)", value)
    if match:
        return match.group(1), int(match.group(2))

    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host.strip(), int(port_text)

    match = re.fullmatch(r"(\S+)\s+(\d+)", value)
    if match:
        return match.group(1).strip("[]"), int(match.group(2))

    raise ValueError(
        f"could not parse target {text!r}; use host:port, [ipv6]:port, "
        "or `nc -v HOST PORT`"
    )


def _state_dir(args: argparse.Namespace) -> Path:
    return Path(args.state_dir).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_run_meta(run_dir: Path) -> dict[str, Any]:
    meta = _read_json(run_dir / "run.json")
    meta.setdefault("session_dir", str(run_dir))
    return meta


def _control_socket_for(run_dir: Path) -> Path | None:
    meta = _read_run_meta(run_dir)
    socket_path = meta.get("control_socket")
    if not socket_path:
        return None
    path = Path(socket_path)
    return path if path.exists() else None


def _all_run_dirs(state_dir: Path, challenge_id: str | None = None) -> list[Path]:
    return [item.root for item in list_run_paths(state_dir, challenge_id) if item.root.exists()]


def _newest_run_dir(state_dir: Path, challenge_id: str | None = None) -> Path | None:
    runs = _all_run_dirs(state_dir, challenge_id)
    if not runs:
        return None

    def sort_key(path: Path) -> float:
        meta = _read_run_meta(path)
        for key in ("updated_at", "started_at"):
            value = meta.get(key)
            if value:
                try:
                    return time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
                except ValueError:
                    pass
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return max(runs, key=sort_key)


def _resolve_run_dir(
    state_dir: Path,
    *,
    challenge_id: str | None,
    run_id: str | None,
) -> Path:
    if challenge_id:
        cid = safe_component(challenge_id, fallback="challenge")
        challenge_dir = state_dir / "challenges" / cid / "runs"
        if run_id:
            run_dir = challenge_dir / safe_component(run_id, fallback="run")
            if not run_dir.exists():
                raise FileNotFoundError(
                    f"run not found: challenge={cid!r} run={run_id!r} ({run_dir})"
                )
            return run_dir
        run_dir = _newest_run_dir(state_dir, cid)
        if run_dir is None:
            raise FileNotFoundError(f"no runs found for challenge {cid!r}")
        return run_dir

    # No explicit selector: use active.json, then the newest run directory.
    active = _read_json(state_dir / "active.json")
    latest = active.get("latest") if isinstance(active.get("latest"), dict) else {}
    candidate = latest.get("run_dir")
    if candidate and Path(candidate).exists():
        return Path(candidate)
    run_dir = _newest_run_dir(state_dir)
    if run_dir is None:
        raise FileNotFoundError(
            f"no bridge runs found under {state_dir}; start one with `serve`"
        )
    return run_dir


def _write_active_latest(state_dir: Path, session: BridgeSession, *, state: str) -> None:
    status = session.status()
    payload = {
        "latest": {
            "challenge_id": status.get("challenge_id"),
            "run_id": status.get("run_id"),
            "session_id": status.get("session_id"),
            "run_dir": status.get("session_dir"),
            "control_socket": status.get("control_socket"),
            "state": state,
            "target": status.get("target"),
            "agent_endpoint": status.get("agent_endpoint"),
            "updated_at": utc_now(),
        },
        "updated_at": utc_now(),
    }
    try:
        write_json_atomic(state_dir / "active.json", payload)
    except OSError:
        pass


def _control_for_run(run_dir: Path) -> Path:
    meta = _read_run_meta(run_dir)
    socket_path = meta.get("control_socket")
    if not socket_path:
        raise ControlError(f"run has no control socket recorded: {run_dir}")
    path = Path(socket_path)
    if not path.exists():
        raise ControlError(f"session is not running (control socket missing): {path}")
    return path


def _format_event(event: dict[str, Any]) -> str:
    direction = event.get("dir", "?")
    if direction == "C->A":
        label = "challenge->agent"
    elif direction == "A->C":
        label = "agent->challenge"
    else:
        label = "system"
    source = event.get("source") or "-"
    length = event.get("len", 0)
    preview = event.get("preview", "")
    ts = event.get("utc", "")
    if ts:
        ts = ts.split("T", 1)[-1].rstrip("Z")
    return (
        f"{ts:>8} [{event.get('seq', 0):>5}] {label:<17} "
        f"{length:>7}B {source:<16} {preview}"
    )


def _print_startup(session: BridgeSession) -> None:
    status = session.status()
    print("")
    print("=" * 78)
    print("IN-CYPHER temporary bridge is running")
    print("=" * 78)
    for line in format_status_lines(status):
        print(line)
    print("-" * 78)
    print("Agent connection (no team key / no PoW needed here):")
    print("  import socket")
    print(
        f"  s = socket.create_connection("
        f"({status.get('bind_host')!r}, {status.get('bind_port')}))"
    )
    print("")
    if status.get("description_present"):
        print(f"Challenge description: {status.get('challenge_md')}")
        print("Preview:")
        for line in str(status.get("description_text", "")).splitlines()[:8]:
            print(f"  {line}")
        print("")
    else:
        print("Challenge description was not provided.  Re-run with:")
        print("  --description-file /path/to/challenge.md")
        print("")
    print("Human inspection:")
    print("  uv run python -m incypher_bridge list")
    print(
        "  uv run python -m incypher_bridge status "
        f"--challenge-id {status.get('challenge_id')} --run-id {status.get('run_id')}"
    )
    print(
        "  uv run python -m incypher_bridge context "
        f"--challenge-id {status.get('challenge_id')} --run-id {status.get('run_id')}"
    )
    print(
        "  uv run python -m incypher_bridge observe --follow "
        f"--challenge-id {status.get('challenge_id')} --run-id {status.get('run_id')}"
    )
    print(f"  tail -f {status.get('transcript_file')}")
    print("")
    print("Human may send bytes to the upstream service (debug only):")
    print(
        "  uv run python -m incypher_bridge send --text 'help\\n' "
        f"--challenge-id {status.get('challenge_id')} --run-id {status.get('run_id')}"
    )
    print("")
    print("Press Ctrl-C to stop the bridge.  Use --duration to change the")
    print("default one-hour time limit.")
    print("=" * 78)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def _load_description(
    args: argparse.Namespace,
    existing: dict[str, Any],
    challenge_md: Path,
) -> tuple[str | None, str | None]:
    """Return ``(description_text, description_file)`` for a challenge."""

    if args.description_text is not None:
        text = str(args.description_text)
        challenge_md.parent.mkdir(parents=True, exist_ok=True)
        challenge_md.write_text(text, encoding="utf-8")
        try:
            os.chmod(challenge_md, 0o600)
        except OSError:
            pass
        return text, None

    if args.description_file:
        path = Path(args.description_file).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"description file not found: {path}")
        text = path.read_text(encoding="utf-8")
        challenge_md.parent.mkdir(parents=True, exist_ok=True)
        challenge_md.write_text(text, encoding="utf-8")
        try:
            os.chmod(challenge_md, 0o600)
        except OSError:
            pass
        return text, str(path)

    # If this challenge already has a description, reuse it.
    text = existing.get("description")
    file_value = existing.get("description_file")
    if text is None and challenge_md.exists():
        try:
            text = challenge_md.read_text(encoding="utf-8")
        except OSError:
            text = None
    return text, file_value


def cmd_serve(args: argparse.Namespace) -> int:
    target_text = args.target
    if not target_text:
        if args.host or args.port:
            if args.host is None or args.port is None:
                _eprint("error: --host and --port must be supplied together")
                return 2
            target_text = f"{args.host}:{args.port}"
        else:
            try:
                target_text = input("Target (host:port or `nc -v HOST PORT`): ").strip()
            except EOFError:
                _eprint("error: no target supplied")
                return 2
    try:
        host, port = parse_target(target_text)
    except ValueError as exc:
        _eprint(f"error: {exc}")
        return 2

    if args.no_timeout:
        duration: float | None = None
    else:
        try:
            duration = parse_duration(args.duration, default=3600)
        except ValueError as exc:
            _eprint(f"error: {exc}")
            return 2

    state_dir = _state_dir(args)
    state_dir.mkdir(parents=True, exist_ok=True)

    challenge_id = args.challenge_id
    if not challenge_id and sys.stdin.isatty():
        try:
            challenge_id = input(
                "Challenge id (short stable name, e.g. overflow-ward): "
            ).strip()
        except EOFError:
            challenge_id = None
    if not challenge_id:
        challenge_id = default_challenge_id(host, port)
        _eprint(
            f"warning: --challenge-id not supplied; using {challenge_id!r}. "
            "Pass --challenge-id to group all runs of the same problem."
        )
    challenge_id = safe_component(challenge_id, fallback="challenge")
    run_id = safe_component(args.run_id or generate_run_id(), fallback="run")

    env_file = find_env_file(args.env_file)
    try:
        team_key = load_team_key(
            env_file=env_file,
            explicit_file=args.team_key_file,
            allow_insecure_file=args.allow_insecure_key_file,
        )
    except TeamKeyError as exc:
        _eprint(f"error: {exc}")
        return 2

    variants: list[PowVariant] | None = None
    if args.pow_variant and args.pow_variant != "auto":
        try:
            variants = _select_variants(args.pow_variant)
        except ValueError as exc:
            _eprint(f"error: {exc}")
            return 2

    challenge_dir = state_dir / "challenges" / challenge_id
    challenge_dir.mkdir(parents=True, exist_ok=True)
    existing_challenge = _read_json(challenge_dir / "challenge.json")
    try:
        description_text, description_file = _load_description(
            args, existing_challenge, challenge_dir / "challenge.md"
        )
    except (OSError, UnicodeDecodeError, FileNotFoundError) as exc:
        _eprint(f"error: cannot load description: {exc}")
        return 2

    challenge_meta = {
        "challenge_id": challenge_id,
        "name": args.challenge_name or existing_challenge.get("name") or challenge_id,
        "category": args.category or existing_challenge.get("category"),
        "description": description_text,
        "description_file": description_file,
        "challenge_md": str(challenge_dir / "challenge.md") if description_text else None,
        "created_at": existing_challenge.get("created_at") or utc_now(),
        "updated_at": utc_now(),
    }
    write_json_atomic(challenge_dir / "challenge.json", challenge_meta)

    paths = RunPaths.create(state_dir, challenge_id=challenge_id, run_id=run_id)
    config = BridgeConfig(
        host=host,
        port=port,
        team_key=team_key,
        duration=duration,
        bind_host=args.bind,
        bind_port=args.listen_port,
        auto_reconnect=not args.no_auto_reconnect,
        reconnect_delay=args.reconnect_delay,
        pow_timeout=args.pow_timeout,
        variants=variants,
        verbose=args.verbose,
        max_pending_bytes=args.max_pending_bytes,
        name=args.name,
        challenge_id=challenge_id,
        run_id=run_id,
        category=challenge_meta.get("category"),
        challenge_name=challenge_meta.get("name"),
        description_file=description_file,
        description_text=description_text,
        env_file=str(env_file),
        metadata={"target": target_text, "description_file": description_file},
    )
    session = BridgeSession(config, paths)
    control = ControlServer(session, paths.control_socket)
    session.add_shutdown_hook(control.shutdown)

    try:
        control.start()
        session.start()
    except Exception as exc:
        _eprint(f"error: {exc}")
        session.stop(reason="startup failed")
        control.shutdown()
        return 1

    _write_active_latest(state_dir, session, state="running")
    if not args.quiet:
        _print_startup(session)

    try:
        while not session.wait(0.25):
            pass
    except KeyboardInterrupt:
        session.stop(reason="keyboard interrupt")
    finally:
        session.stop(reason="shutdown")
        control.shutdown()
        _write_active_latest(state_dir, session, state="stopped")
        status = session.status()
        if not args.quiet:
            print("")
            print(f"[bridge] stopped: {status.get('stop_reason') or 'done'}")
            print(f"[bridge] run dir: {status.get('session_dir')}")
    return 0


# ---------------------------------------------------------------------------
# list / status / context
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    run_dirs = _all_run_dirs(state_dir, args.challenge_id)
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        meta = _read_run_meta(run_dir)
        rows.append(
            {
                "challenge_id": meta.get("challenge_id") or run_dir.parent.parent.name,
                "run_id": meta.get("run_id") or run_dir.name,
                "state": meta.get("state", "unknown"),
                "target": meta.get("target"),
                "agent_endpoint": meta.get("agent_endpoint"),
                "run_dir": str(run_dir),
                "updated_at": meta.get("updated_at") or meta.get("started_at"),
                "description_present": bool(meta.get("description_present")),
                "control_socket": meta.get("control_socket"),
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0

    if not rows:
        print(f"(no runs under {state_dir})")
        return 0

    header = (
        f"{'CHALLENGE':<22} {'RUN':<36} {'STATE':<12} "
        f"{'AGENT':<22} {'TARGET':<24} DESCRIPTION"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{str(row['challenge_id'])[:22]:<22} "
            f"{str(row['run_id'])[:36]:<36} "
            f"{str(row['state'])[:12]:<12} "
            f"{str(row['agent_endpoint'] or '-')[:22]:<22} "
            f"{str(row['target'] or '-')[:24]:<24} "
            f"{'yes' if row['description_present'] else 'no'}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
    except FileNotFoundError as exc:
        _eprint(f"error: {exc}")
        return 1

    meta = _read_run_meta(run_dir)
    status: dict[str, Any] | None = None
    socket_path = meta.get("control_socket")
    if socket_path and Path(socket_path).exists():
        try:
            response = control_request(socket_path, "status")
            if response.get("ok"):
                status = response.get("status")
        except ControlError:
            status = None
    if status is None:
        status = meta

    for line in format_status_lines(status):
        print(line)
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
    except FileNotFoundError as exc:
        _eprint(f"error: {exc}")
        return 1

    meta = _read_run_meta(run_dir)
    description = meta.get("description_text")
    challenge_md = meta.get("challenge_md")
    if not description and challenge_md and Path(challenge_md).exists():
        try:
            description = Path(challenge_md).read_text(encoding="utf-8")
        except OSError:
            description = None

    context = {
        "challenge_id": meta.get("challenge_id"),
        "run_id": meta.get("run_id"),
        "challenge_name": meta.get("challenge_name"),
        "category": meta.get("category"),
        "description": description or "",
        "description_file": meta.get("description_file"),
        "challenge_md": challenge_md,
        "target": meta.get("target"),
        "agent_endpoint": meta.get("agent_endpoint"),
        "state": meta.get("state"),
        "control_socket": meta.get("control_socket"),
        "run_dir": str(run_dir),
        "pow_summary": meta.get("pow_summary"),
    }
    if args.json:
        print(json.dumps(context, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"Challenge:   {context['challenge_id']}")
    print(f"Run:         {context['run_id']}")
    print(f"Name:        {context['challenge_name'] or '-'}")
    print(f"Category:    {context['category'] or '-'}")
    print(f"Target:      {context['target'] or '-'}")
    print(f"Agent:       {context['agent_endpoint'] or '-'}")
    print(f"State:       {context['state'] or '-'}")
    print(f"PoW:         {context['pow_summary'] or '-'}")
    print(f"Description file: {context['description_file'] or '-'}")
    print(f"Challenge md:     {challenge_md or '-'}")
    print("-" * 78)
    if description:
        print(description.rstrip())
    else:
        print("(no challenge description provided)")
    return 0


# ---------------------------------------------------------------------------
# observe / send / reconnect / stop
# ---------------------------------------------------------------------------
def cmd_observe(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
    except FileNotFoundError as exc:
        _eprint(f"error: {exc}")
        return 1

    since = 0
    try:
        while True:
            events = EventLogger.read_events(
                run_dir / "events.jsonl", since=since, limit=args.limit
            )
            for event in events:
                print(_format_event(event))
                since = max(since, int(event.get("seq", 0)))
            if not args.follow:
                return 0
            meta = _read_run_meta(run_dir)
            if str(meta.get("state")) in {"stopped", "stopped/unavailable"} and not events:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_send(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
        socket_path = _control_for_run(run_dir)
    except (FileNotFoundError, ControlError) as exc:
        _eprint(f"error: {exc}")
        return 1

    if args.hex is not None:
        try:
            data = bytes.fromhex(args.hex)
        except ValueError as exc:
            _eprint(f"error: invalid hex: {exc}")
            return 2
    elif args.text is not None:
        data = args.text.encode("utf-8")
    elif args.file is not None:
        data = Path(args.file).read_bytes()
    else:
        _eprint("error: provide --text, --hex, or --file")
        return 2

    try:
        response = control_request(
            socket_path,
            "send",
            data_b64=base64.b64encode(data).decode("ascii"),
            label="human-cli",
        )
    except ControlError as exc:
        _eprint(f"error: {exc}")
        return 1
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def cmd_reconnect(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
        socket_path = _control_for_run(run_dir)
        response = control_request(socket_path, "reconnect")
    except (FileNotFoundError, ControlError) as exc:
        _eprint(f"error: {exc}")
        return 1
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def cmd_stop(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    try:
        run_dir = _resolve_run_dir(
            state_dir, challenge_id=args.challenge_id, run_id=args.run_id
        )
        socket_path = _control_for_run(run_dir)
        response = control_request(socket_path, "stop", reason="cli stop")
    except (FileNotFoundError, ControlError) as exc:
        _eprint(f"error: {exc}")
        return 1
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


def cmd_pow_solve(args: argparse.Namespace) -> int:
    from .pow import PowChallenge

    nonce = args.nonce
    if nonce is None:
        nonce = input("nonce: ").strip()
    try:
        team_key = load_team_key(
            env_file=find_env_file(args.env_file),
            explicit_file=args.team_key_file,
            allow_insecure_file=args.allow_insecure_key_file,
        )
    except TeamKeyError as exc:
        _eprint(f"error: {exc}")
        return 2
    try:
        challenge = PowChallenge(nonce_hex=nonce, bits=args.bits)
        variant = DEFAULT_VARIANTS[0]
        if args.variant != "auto":
            variant = _select_variants(args.variant)[0]
    except ValueError as exc:
        _eprint(f"error: {exc}")
        return 2
    try:
        start = time.monotonic()
        x = solve_pow(challenge, team_key, variant=variant, timeout=args.timeout)
        elapsed = time.monotonic() - start
    except PowError as exc:
        _eprint(f"error: {exc}")
        return 1
    print(x.decode("utf-8", errors="replace"))
    _eprint(f"[pow] solved in {elapsed:.3f}s using {variant.name}")
    return 0


def _select_variants(name: str) -> list[PowVariant]:
    mapping = {
        "rawmac-ascii-nonce/decimal-x": "rawmac-ascii-nonce/decimal-x",
        "rawmac-raw-nonce/decimal-x": "rawmac-raw-nonce/decimal-x",
        "rawmac-ascii-nonce/hex-x": "rawmac-ascii-nonce/hex-x",
        "rawmac-raw-nonce/hex-x": "rawmac-raw-nonce/hex-x",
        "hexmac-ascii-nonce/decimal-x": "hexmac-ascii-nonce/decimal-x",
        "hexmac-raw-nonce/decimal-x": "hexmac-raw-nonce/decimal-x",
        "hexmac-ascii-nonce/hex-x": "hexmac-ascii-nonce/hex-x",
        "hexmac-raw-nonce/hex-x": "hexmac-raw-nonce/hex-x",
        "decimal": "rawmac-ascii-nonce/decimal-x",
        "hex": "rawmac-ascii-nonce/hex-x",
        "rawmac-decimal": "rawmac-ascii-nonce/decimal-x",
        "rawmac-hex": "rawmac-ascii-nonce/hex-x",
    }
    wanted = mapping.get(name)
    if wanted is None:
        raise ValueError(f"unknown PoW variant {name!r}")
    for variant in DEFAULT_VARIANTS:
        if variant.name == wanted:
            return [variant]
    raise ValueError(f"PoW variant {name!r} is not available")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _add_selector(parser: argparse.ArgumentParser, *, include_state: bool = True) -> None:
    parser.add_argument("--challenge-id", help="challenge id, e.g. overflow-ward")
    parser.add_argument("--run-id", help="run id, e.g. run-20260916T145544Z-2da959")
    if include_state:
        parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incypher-bridge",
        description=(
            "Temporary IN-CYPHER middleware: reads .env.local, clears the "
            "team-key-bound PoW gate, and exposes a local maintainable TCP "
            "endpoint for an autonomous agent plus human inspection commands."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="start a bridge run")
    serve.add_argument("--target", help="host:port or `nc -v HOST PORT`")
    serve.add_argument("--host", help="challenge host (alternative to --target)")
    serve.add_argument("--port", type=int, help="challenge port (alternative to --target)")
    serve.add_argument("--challenge-id", help="stable challenge id, e.g. overflow-ward")
    serve.add_argument("--run-id", help="optional run id; auto-generated by default")
    serve.add_argument("--challenge-name", help="human-friendly challenge name")
    serve.add_argument(
        "--category",
        help="challenge category, e.g. pwn/web/network/crypto/rev/forensics/misc",
    )
    serve.add_argument(
        "--description-file",
        help="optional local .md/.txt file with the challenge description",
    )
    serve.add_argument(
        "--description-text",
        help="inline challenge description (alternative to --description-file)",
    )
    serve.add_argument(
        "--duration",
        default="1h",
        help="run time limit: 3600, 90m, 1h, 1h30m (default: 1h)",
    )
    serve.add_argument("--no-timeout", action="store_true", help="disable the time limit")
    serve.add_argument("--bind", default="127.0.0.1", help="local bind host (default: 127.0.0.1)")
    serve.add_argument(
        "--listen-port",
        type=int,
        default=0,
        help="local agent port; 0 chooses a free port automatically (default: 0)",
    )
    serve.add_argument("--name", help="optional alias stored in metadata")
    serve.add_argument("--env-file", default=str(find_env_file(None)), help="private env file")
    serve.add_argument("--team-key-file", help="private file containing only the team key")
    serve.add_argument(
        "--allow-insecure-key-file",
        action="store_true",
        help="allow env/key files that are group/world-readable (not recommended)",
    )
    serve.add_argument(
        "--pow-timeout",
        type=float,
        default=180.0,
        help="maximum seconds to solve each PoW variant (default: 180)",
    )
    serve.add_argument(
        "--pow-variant",
        default="auto",
        help="auto (default) or a name from `incypher_bridge.pow.DEFAULT_VARIANTS`",
    )
    serve.add_argument(
        "--no-auto-reconnect",
        action="store_true",
        help="do not reconnect the upstream automatically after a drop",
    )
    serve.add_argument(
        "--reconnect-delay",
        type=float,
        default=1.0,
        help="seconds between reconnection attempts (default: 1)",
    )
    serve.add_argument(
        "--max-pending-bytes",
        type=int,
        default=1024 * 1024,
        help="bounded buffer for upstream bytes received while no agent is attached",
    )
    serve.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="bridge state directory")
    serve.add_argument("--verbose", action="store_true", help="show PoW variant progress")
    serve.add_argument("--quiet", action="store_true", help="suppress startup banner")

    list_parser = subparsers.add_parser("list", help="list challenges and runs")
    list_parser.add_argument("--challenge-id", help="filter by challenge id")
    list_parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    list_parser.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show a run summary")
    _add_selector(status)
    status.add_argument("--json", action="store_true", help="also print machine-readable JSON")

    context = subparsers.add_parser("context", help="print challenge description and run context")
    _add_selector(context)
    context.add_argument("--json", action="store_true", help="print machine-readable JSON")

    observe = subparsers.add_parser("observe", help="print human-readable event transcript")
    _add_selector(observe)
    observe.add_argument("--follow", "-f", action="store_true", help="follow new events")
    observe.add_argument("--limit", type=int, default=200, help="max events per poll")
    observe.add_argument("--interval", type=float, default=0.5, help="follow poll interval")

    send = subparsers.add_parser("send", help="send human debugging bytes to the upstream")
    _add_selector(send)
    send.add_argument("--text", help="UTF-8 text to send")
    send.add_argument("--hex", help="hex bytes to send")
    send.add_argument("--file", help="file whose bytes to send")

    reconnect = subparsers.add_parser("reconnect", help="force upstream reconnection")
    _add_selector(reconnect)

    stop = subparsers.add_parser("stop", help="stop a running bridge run")
    _add_selector(stop)

    pow_solve = subparsers.add_parser("pow-solve", help="offline PoW calculator (no connection)")
    pow_solve.add_argument("--nonce", help="nonce text from the gate banner")
    pow_solve.add_argument("--bits", type=int, required=True, help="leading zero bits")
    pow_solve.add_argument(
        "--variant",
        default="auto",
        help="auto or a PoW variant name (default: auto = first/most likely)",
    )
    pow_solve.add_argument("--timeout", type=float, default=180.0)
    pow_solve.add_argument("--env-file", default=str(find_env_file(None)))
    pow_solve.add_argument("--team-key-file")
    pow_solve.add_argument("--allow-insecure-key-file", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    command = args.command
    if command == "serve":
        return cmd_serve(args)
    if command == "list":
        return cmd_list(args)
    if command == "status":
        return cmd_status(args)
    if command == "context":
        return cmd_context(args)
    if command == "observe":
        return cmd_observe(args)
    if command == "send":
        return cmd_send(args)
    if command == "reconnect":
        return cmd_reconnect(args)
    if command == "stop":
        return cmd_stop(args)
    if command == "pow-solve":
        return cmd_pow_solve(args)
    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
