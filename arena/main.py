"""Arena container entrypoint: schedule challenges concurrently.

Policy:
- Dynamic challenges are globally serialized: at most one live instance.
- Static challenges may run concurrently.
- Static challenge files are preloaded before solving starts.
- Challenges are attempted in points-ascending order.
- Default challenge concurrency is 2, configurable via MAX_CONCURRENT_CHALLENGES.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .platform import connect_from_env, mode_from_env, select_targets
from .solver import prepare_files, solve_challenge


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    return int(raw)


def _split_env(name: str, cast=str) -> list[Any]:
    raw = os.environ.get(name, "")
    return [cast(part) for part in raw.replace(",", " ").split() if part]


def _is_dynamic(ch: dict[str, Any]) -> bool:
    return str(ch.get("type") or "") == "dynamic_iac"


def _timing_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result.get("id"),
        "name": result.get("name"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "seconds": result.get("seconds"),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _results_payload(
    mode: str,
    results: list[dict[str, Any]],
    *,
    total_seconds: float | None = None,
) -> dict[str, Any]:
    ordered = sorted(results, key=lambda row: int(row.get("id") or 0))
    payload: dict[str, Any] = {
        "mode": mode,
        "submit_flags": _env_bool("SUBMIT_FLAGS", True),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attempted": len(ordered),
        "solved": sum(1 for row in ordered if row.get("solved")),
        "results": ordered,
        "timings": [_timing_row(row) for row in ordered],
    }
    if total_seconds is not None:
        payload["total_seconds"] = total_seconds
    return payload


def _preload_static(
    client,
    ch: dict[str, Any],
    work_root: Path,
) -> tuple[int, list[str]]:
    cid = int(ch["id"])
    cdir = work_root / str(cid)
    return cid, prepare_files(client, ch, cdir)


def _run_challenge(
    client,
    ch: dict[str, Any],
    *,
    work_root: Path,
    max_steps: int,
    max_attempts: int,
    prepared_filenames: list[str] | None,
    dynamic_lock: threading.Lock,
) -> dict[str, Any]:
    cid = int(ch["id"])
    kwargs = dict(
        max_steps=max_steps,
        max_attempts=max_attempts,
        work_root=work_root,
        events_path=work_root / str(cid) / "events.jsonl",
        prepared_filenames=prepared_filenames,
    )
    if _is_dynamic(ch):
        with dynamic_lock:
            return solve_challenge(client, ch, **kwargs)
    return solve_challenge(client, ch, **kwargs)


def main() -> int:
    client = connect_from_env()
    mode = mode_from_env()
    work_root = Path(os.environ.get("WORK_ROOT", "/work")).expanduser().resolve()
    results_path = Path(
        os.environ.get("RESULTS_PATH", str(work_root / "results.json"))
    ).expanduser().resolve()
    max_steps = _env_int("MAX_STEPS", 1_000_000)
    max_attempts = _env_int("MAX_ATTEMPTS", 3)
    max_concurrent = max(1, _env_int("MAX_CONCURRENT_CHALLENGES", 2))
    preload_workers = max(1, _env_int("PRELOAD_WORKERS", max_concurrent))
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
                "max_concurrent_challenges": max_concurrent,
                "ids": [int(ch["id"]) for ch in targets],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    prepared: dict[int, list[str]] = {}
    static_targets = [ch for ch in targets if not _is_dynamic(ch)]
    if static_targets:
        print(
            json.dumps(
                {"event": "preload_start", "static_count": len(static_targets)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        with futures.ThreadPoolExecutor(max_workers=preload_workers) as pool:
            future_map = {
                pool.submit(_preload_static, client, ch, work_root): ch
                for ch in static_targets
            }
            for future in futures.as_completed(future_map):
                ch = future_map[future]
                cid = int(ch["id"])
                try:
                    _, filenames = future.result()
                except Exception as exc:  # noqa: BLE001 - report as a missing file
                    cdir = work_root / str(cid)
                    cdir.mkdir(parents=True, exist_ok=True)
                    filenames = [f"(preload failed: {type(exc).__name__}: {exc})"]
                prepared[cid] = filenames
                print(
                    json.dumps(
                        {
                            "event": "preload_finish",
                            "id": cid,
                            "files": filenames,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    results: list[dict[str, Any]] = []
    run_started = time.perf_counter()
    dynamic_lock = threading.Lock()
    with futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        future_map = {
            pool.submit(
                _run_challenge,
                client,
                ch,
                work_root=work_root,
                max_steps=max_steps,
                max_attempts=max_attempts,
                prepared_filenames=prepared.get(int(ch["id"])),
                dynamic_lock=dynamic_lock,
            ): ch
            for ch in targets
        }
        for future in futures.as_completed(future_map):
            ch = future_map[future]
            cid = int(ch["id"])
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - never lose the scheduler
                result = {
                    "id": cid,
                    "name": ch.get("name"),
                    "category": ch.get("category"),
                    "points": ch.get("points"),
                    "type": ch.get("type"),
                    "solved": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            payload = _results_payload(mode, results)
            _write_json(results_path, payload)
            print(
                json.dumps(
                    {
                        "event": "challenge_finish",
                        "id": cid,
                        "name": ch.get("name"),
                        "solved": result.get("solved"),
                        "steps": result.get("steps"),
                        "seconds": result.get("seconds"),
                        "started_at": result.get("started_at"),
                        "finished_at": result.get("finished_at"),
                        "error": result.get("error"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    total = round(time.perf_counter() - run_started, 1)
    payload = _results_payload(mode, results, total_seconds=total)
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
