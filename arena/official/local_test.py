#!/usr/bin/env python3
"""Local smoke-test for your Brain — no platform access needed.

Runs your brain.py's Brain.solve() against a sample prompt with a real run_bash and a
mock submit_flag, so you can iterate on strategy before you push an image.

    python3 /opt/agent/local_test.py

The reference brain.py is an LLM agent, so it needs LLM_API_KEY here. The arena injects
one at run time; locally you supply your own (any OpenAI-compatible endpoint). A scripted
Brain that never calls an LLM needs no key at all.
"""
import os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brain import Brain  # noqa: E402


def run_bash(cmd: str) -> str:
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        return out if out.strip() else "(no output, exit=%d)" % p.returncode
    except subprocess.TimeoutExpired:
        return "(command timed out after 120s)"


def submit_flag(flag: str) -> dict:
    print("\n[local_test] >>> your agent submitted: %r" % flag)
    print("[local_test]     (local test cannot verify flags)")
    return {"status": "incorrect", "note": "not verified locally"}


SAMPLE = """# Challenge: Warmup Web
Category: web   Points: 100   Type: dynamic_iac

## Description
A tiny web app is running. Find the flag (format INCYPHER{...}).

## Live instance connection info
http://example.com/   (sample only)

Solve it, then submit the flag with submit_flag.
"""

if __name__ == "__main__":
    if not os.environ.get("LLM_API_KEY"):
        print("!! LLM_API_KEY is not set.")
        print("!! The reference brain.py calls an LLM, so it will fail with HTTP 401 below.")
        print("!! That is the missing-key signature, not a bug in your code.")
        print("!!   export LLM_API_KEY=...   (plus LLM_BASE_URL / LLM_MODEL if not OpenRouter)")
        print("!! The arena injects a key at run time. A scripted Brain needs no key at all.\n")
    steps = int(os.environ.get("MAX_STEPS", "8"))
    print("=== local_test: running your Brain for up to %d steps ===" % steps)
    print("\n=== result ===")
    print(Brain(run_bash=run_bash, submit_flag=submit_flag, max_steps=steps).solve(SAMPLE))
