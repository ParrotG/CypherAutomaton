# IN-CYPHER Agent Arena — agent image

Everything you need is in this directory. Nothing is downloaded separately, so nothing can
drift from what the arena actually runs.

**Read `CONTRACT.md` first** — the full spec: the `Brain` interface, the environment injected
at run time, the sandbox your agent runs in, how to submit, and the rules.

| File | What it is |
|---|---|
| `CONTRACT.md` | **The contract.** Start here. |
| `brain.py` | **The file you replace** — a complete, working reference agent. |
| `ctfd.py` | Platform client (auth headers handled; `connect_pwn()` solves the PoW gate). |
| `main.py`, `solver.py` | The harness that calls your `Brain`. |
| `local_test.py` | Exercise your `Brain` with no platform access. |
| `check_agent.sh` | The same image check the organizers run. A local PASS means it will run. |

Typical loop:

```bash
export LLM_API_KEY=...                 # only if your Brain calls an LLM (the reference one does)
python3 /opt/agent/local_test.py       # iterate on logic
docker build --platform linux/amd64 -t my-agent .
/opt/agent/check_agent.sh my-agent:latest
docker push registry.in-cypher.com:5001/team-<id>/agent:latest
```

Live instructions: https://hackathonlive.in-cypher.com/usage
