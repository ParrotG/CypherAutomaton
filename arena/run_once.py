"""Run one official challenge end to end.

This is the stage-3 harness used before the custom global scheduler exists.
It uses the official CTF agent API and the arena Brain.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .platform import connect_from_env
from .solver import default_work_root, solve_challenge


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arena-run-once")
    parser.add_argument("--challenge-id", type=int, required=True)
    parser.add_argument("--max-steps", type=int, default=int(os.environ.get("MAX_STEPS", "1000000")))
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(os.environ.get("MAX_ATTEMPTS", "3")),
    )
    parser.add_argument(
        "--work-root",
        default=None,
        help="Work root (default: $WORK_ROOT/$ARENA_WORK_ROOT, else /work or a temp fallback)",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    client = connect_from_env()
    ch = client.challenge(args.challenge_id)
    if not ch:
        print(json.dumps({"ok": False, "error": f"challenge {args.challenge_id} not found"}))
        return 1

    work_root = (
        Path(args.work_root).expanduser().resolve()
        if args.work_root
        else default_work_root()
    )
    result = solve_challenge(
        client,
        ch,
        max_steps=args.max_steps,
        max_attempts=args.max_attempts,
        work_root=work_root,
        events_path=work_root / str(args.challenge_id) / "events.jsonl",
    )
    out = Path(args.out).expanduser().resolve() if args.out else work_root / f"result-{args.challenge_id}.json"
    _write_json(out, result)

    summary = {
        "ok": True,
        "challenge_id": args.challenge_id,
        "name": result.get("name"),
        "solved": result.get("solved"),
        "steps": result.get("steps"),
        "seconds": result.get("seconds"),
        "attempts": result.get("attempt_count"),
        "error": result.get("error"),
        "out": str(out),
        "events": str(work_root / str(args.challenge_id) / "agents"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.get("solved") else 10


if __name__ == "__main__":
    raise SystemExit(main())
