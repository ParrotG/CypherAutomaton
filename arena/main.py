"""Arena container entrypoint: attempt the selected challenges sequentially.

This is intentionally a small global scheduler. It uses the official CTF agent
API, orders challenges by points ascending, and writes partial results after each
challenge. Concurrency and dynamic-instance locking can be added later.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .platform import connect_from_env, mode_from_env, select_targets
from .solver import solve_challenge


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _split_env(name: str, cast=str) -> list[Any]:
    raw = os.environ.get(name, "")
    return [cast(part) for part in raw.replace(",", " ").split() if part]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    client = connect_from_env()
    mode = mode_from_env()
    work_root = Path(os.environ.get("WORK_ROOT", "/work")).expanduser().resolve()
    results_path = Path(
        os.environ.get("RESULTS_PATH", str(work_root / "results.json"))
    ).expanduser().resolve()
    max_steps = int(os.environ.get("MAX_STEPS", "1000000"))
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    only_ids = _split_env("ONLY_IDS", int)
    categories = _split_env("CATEGORIES", str)

    me = client.me()
    solved_ids = {int(cid) for cid in (me.get("solved") or [])}
    include_solved = _env_bool("INCLUDE_SOLVED", default=(mode == "practice"))
    targets = select_targets(
        client.list_challenges(),
        mode=mode,
        solved_ids=solved_ids,
        include_solved=include_solved,
        only_ids=only_ids,
        categories=categories,
    )

    print(
        json.dumps(
            {
                "event": "scheduler_start",
                "mode": mode,
                "include_solved": include_solved,
                "work_root": str(work_root),
                "results_path": str(results_path),
                "challenge_count": len(targets),
                "ids": [int(ch["id"]) for ch in targets],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, ch in enumerate(targets, 1):
        cid = int(ch["id"])
        print(
            json.dumps(
                {"event": "challenge_start", "index": index, "id": cid, "name": ch.get("name")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        result = solve_challenge(
            client,
            ch,
            max_steps=max_steps,
            max_attempts=max_attempts,
            work_root=work_root,
            events_path=work_root / str(cid) / "events.jsonl",
        )
        results.append(result)
        payload = {
            "mode": mode,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "attempted": len(results),
            "solved": sum(1 for row in results if row.get("solved")),
            "results": results,
        }
        _write_json(results_path, payload)
        print(
            json.dumps(
                {
                    "event": "challenge_finish",
                    "index": index,
                    "id": cid,
                    "solved": result.get("solved"),
                    "steps": result.get("steps"),
                    "seconds": result.get("seconds"),
                    "error": result.get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    total = round(time.perf_counter() - started, 1)
    payload = {
        "mode": mode,
        "total_seconds": total,
        "attempted": len(results),
        "solved": sum(1 for row in results if row.get("solved")),
        "results": results,
    }
    _write_json(results_path, payload)
    print(
        json.dumps(
            {
                "event": "scheduler_finish",
                "attempted": payload["attempted"],
                "solved": payload["solved"],
                "total_seconds": total,
                "results_path": str(results_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
