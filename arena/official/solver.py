"""Per-challenge orchestration: prepare context, run the brain, time it."""
from __future__ import annotations
import os, subprocess, time
from ctfd import CTFdClient
from brain import Brain

WORK = "/work"


def run_bash(cmd: str) -> str:
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                           text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        return out if out.strip() else f"(no output, exit={p.returncode})"
    except subprocess.TimeoutExpired:
        return "(command timed out after 120s)"
    except Exception as e:  # noqa
        return f"(execution error: {e})"


def prepare_files(client: CTFdClient, ch: dict, cdir: str) -> list[str]:
    os.makedirs(cdir, exist_ok=True)
    names = []
    for url in ch.get("files") or []:
        base = os.path.basename(url.split("?")[0])
        try:
            client.download(url, os.path.join(cdir, base))
            names.append(base)
        except Exception as e:  # noqa
            names.append(f"{base} (download failed: {e})")
    return names


def boot_instance(client: CTFdClient, cid: int) -> str | None:
    data = client.boot(cid)
    if data.get("error"):
        # maybe already running
        data = client.instance_status(cid)
    info = data.get("connectionInfo")
    if info:
        return info
    # give it a few seconds to come up
    for _ in range(6):
        time.sleep(5)
        info = client.instance_status(cid).get("connectionInfo")
        if info:
            return info
    return None


def build_prompt(ch: dict, cdir: str, filenames: list[str], conn: str | None) -> str:
    lines = [
        f"# Challenge: {ch['name']}",
        f"Category: {ch.get('category')}   Points: {ch.get('value')}   "
        f"Type: {ch.get('type')}",
        "",
        "## Description",
        (ch.get("description") or "(none)").strip(),
        "",
    ]
    if filenames:
        lines += [f"## Files (in {cdir}/)", *[f"- {n}" for n in filenames], ""]
    if conn:
        lines += ["## Live instance connection info", conn, ""]
        if "PoW-gated" in conn:
            lines += [
                "This service sits behind a proof-of-work gate. Do NOT implement the gate "
                "yourself and do NOT connect with plain nc — use the helper that ships in "
                "this image, which solves it for you:",
                "",
                "```python",
                "import sys; sys.path.insert(0, '/opt/agent')",
                "from ctfd import connect_pwn",
                "sock = connect_pwn(host, port, team_key)   # both appear in the line above",
                "sock.sendall(b'...'); print(sock.recv(4096))",
                "```",
                "",
            ]
    lines += ["Solve it, then submit the flag with submit_flag."]
    return "\n".join(lines)


def solve_challenge(client: CTFdClient, ch: dict, max_steps: int) -> dict:
    cid = ch["id"]
    cdir = os.path.join(WORK, str(cid))
    filenames = prepare_files(client, ch, cdir)
    conn = None
    if ch.get("type") == "dynamic_iac":
        conn = boot_instance(client, cid)
    prompt = build_prompt(ch, cdir, filenames, conn)

    brain = Brain(run_bash=run_bash,
                  submit_flag=lambda f: client.submit(cid, f),
                  max_steps=max_steps)
    t0 = time.perf_counter()
    try:
        result = brain.solve(prompt)
    except Exception as e:  # noqa
        result = {"solved": False, "error": f"{type(e).__name__}: {e}"}
    dt = time.perf_counter() - t0

    if ch.get("type") == "dynamic_iac":
        try:
            client.destroy(cid)
        except Exception:
            pass
    return {
        "id": cid, "name": ch["name"], "category": ch.get("category"),
        "type": ch.get("type"), "had_files": bool(filenames),
        "had_instance": bool(conn), "seconds": round(dt, 1),
        **result,
    }
