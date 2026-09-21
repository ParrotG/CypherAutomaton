#!/usr/bin/env python3
"""Entrypoint: run the autonomous agent over the platform's NON-practice challenges.

Environment:
  CTF_BASE          base URL (e.g. https://hackathon.in-cypher.com)
  CTF_TOKEN         per-team CTFd Access Token  (Authorization: Token <token>)
                    -- this replaces the old session-cookie auth. No login / no cookie / no CSRF.
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL   agent brain (OpenAI-compatible; DeepSeek/OpenRouter)
  MAX_STEPS         per-challenge LLM step budget (default 40)
  ONLY_IDS          optional comma-separated challenge ids to restrict the run
  CATEGORIES        optional comma-separated base categories to include
Writes /work/results.json and prints a per-challenge timing table.
"""
from __future__ import annotations
import json, os, sys, time
from ctfd import CTFdClient
from solver import solve_challenge

WORK = "/work"


def is_practice(ch: dict) -> bool:
    return "practice" in (ch.get("category") or "").lower()


def main() -> int:
    base = os.environ["CTF_BASE"]
    token = os.environ.get("CTF_TOKEN") or os.environ.get("CTF_SESSION")
    if not token:
        print("FATAL: set CTF_TOKEN (per-team CTFd access token)", file=sys.stderr)
        return 2
    max_steps = int(os.environ.get("MAX_STEPS", "40"))
    only = {int(x) for x in os.environ.get("ONLY_IDS", "").replace(",", " ").split() if x}
    cats = {c.strip().lower() for c in os.environ.get("CATEGORIES", "").split(",") if c.strip()}

    client = CTFdClient(base, token)
    allch = client.list_challenges()
    targets = [c for c in allch if not is_practice(c)]
    if only:
        targets = [c for c in targets if c["id"] in only]
    if cats:
        targets = [c for c in targets if (c.get("category") or "").lower() in cats]
    targets.sort(key=lambda c: c["id"])

    print(f"=== {len(targets)} non-practice challenges to attempt "
          f"(of {len(allch)} total) · auth=token ===", flush=True)

    results = []
    run_t0 = time.perf_counter()
    for i, brief in enumerate(targets, 1):
        cid = brief["id"]
        print(f"\n[{i}/{len(targets)}] id={cid} {brief.get('category'):12} "
              f"{brief['name']}", flush=True)
        ch = client.challenge(cid)
        res = solve_challenge(client, ch, max_steps)
        tag = "SOLVED" if res.get("solved") else "unsolved"
        print(f"   -> {tag} in {res['seconds']}s ({res.get('steps','?')} steps)", flush=True)
        results.append(res)
        json.dump(results, open(f"{WORK}/results.json", "w"), indent=2)

    total = round(time.perf_counter() - run_t0, 1)
    solved = [r for r in results if r.get("solved")]
    print("\n" + "=" * 64)
    print(f"DONE: {len(solved)}/{len(results)} solved, total wall time {total}s")
    json.dump({"total_seconds": total, "solved": len(solved),
               "attempted": len(results), "results": results},
              open(f"{WORK}/results.json", "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
