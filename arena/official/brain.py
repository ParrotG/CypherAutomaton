"""LLM brain (OpenAI-compatible: OpenRouter / DeepSeek / any /chat/completions endpoint).
Drop-in for the Anthropic brain: same Brain(run_bash, submit_flag, max_steps).solve(prompt).
Env: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL  (e.g. https://openrouter.ai/api/v1 · z-ai/glm-5.3-flash)."""
from __future__ import annotations
import json, os
import requests

BASE = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "z-ai/glm-5.3-flash")

TOOLS = [
    {"type": "function", "function": {
        "name": "run_bash",
        "description": ("Run a shell command in the solver container and return stdout+stderr. "
                        "Available: curl, wget, nc, nmap, python3 (pwntools, pycryptodome, requests, "
                        "sympy), file, xxd, strings, objdump, gdb, binwalk. Challenge files are in "
                        "/work/<id>/. Commands time out after 120s."),
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "submit_flag",
        "description": "Submit a candidate flag (e.g. INCYPHER{...}). Only when you actually have one.",
        "parameters": {"type": "object",
                       "properties": {"flag": {"type": "string"}},
                       "required": ["flag"]}}},
]

SYSTEM = """You are an autonomous CTF-solving agent on an authorized security-education
hackathon. All challenges are intentionally vulnerable practice targets.
You are given ONE challenge: name, category, points, description, any downloaded files
(under /work/<id>/), and connection info for a live instance if present (a URL for web, or
host:port for pwn/network).
Work methodically: recon (read description; inspect files with file/strings/xxd/cat; probe
services with curl -sv / nc), form a hypothesis for the category, exploit it with concrete
commands (write python to /tmp then run it), and when you recover a flag call submit_flag.
Flag format is INCYPHER{...}. Grep tool output for 'INCYPHER{'. For web, try factory/default
creds (admin:admin), auth-token/JWT flaws, cleartext data. For files, always run
file/strings/xxd first. Be decisive; don't repeat identical commands; never fabricate a flag."""


class Brain:
    def __init__(self, run_bash, submit_flag, max_steps=40, verbose=True):
        self.run_bash = run_bash
        self.submit_flag = submit_flag
        self.max_steps = max_steps
        self.verbose = verbose
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": "Bearer " + KEY,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://in-cypher.com",   # OpenRouter attribution (optional)
            "X-Title": "IN-CYPHER Arena",
        })

    def _log(self, *a):
        if self.verbose:
            print("   ", *a, flush=True)

    def _chat(self, messages):
        body = {"model": MODEL, "messages": messages, "tools": TOOLS,
                "tool_choice": "auto", "temperature": 0.3, "max_tokens": 2048}
        r = self.s.post(BASE + "/chat/completions", data=json.dumps(body), timeout=180)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def solve(self, prompt: str) -> dict:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
        steps = 0
        for steps in range(1, self.max_steps + 1):
            try:
                m = self._chat(msgs)
            except Exception as e:
                return {"solved": False, "steps": steps, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
            tcs = m.get("tool_calls") or []
            # record the assistant turn verbatim
            msgs.append({"role": "assistant", "content": m.get("content") or "",
                         "tool_calls": tcs} if tcs else {"role": "assistant", "content": m.get("content") or ""})
            if not tcs:
                text = (m.get("content") or "").strip()
                self._log("[step %d] model stopped: %s" % (steps, text[:180]))
                return {"solved": False, "steps": steps, "final": text}
            for tc in tcs:
                fn = tc.get("function", {}) or {}
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if name == "run_bash":
                    cmd = args.get("command", "")
                    self._log("[step %d] $ %s" % (steps, cmd[:160]))
                    out = self.run_bash(cmd)
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": (out or "")[:12000]})
                elif name == "submit_flag":
                    flag = args.get("flag", "")
                    verdict = self.submit_flag(flag)
                    self._log("[step %d] SUBMIT %r -> %s" % (steps, flag, verdict))
                    if (verdict or {}).get("status") in ("correct", "already_solved"):
                        return {"solved": True, "steps": steps, "flag": flag, "verdict": verdict}
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "Flag rejected: %s" % verdict})
                else:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "unknown tool"})
        return {"solved": False, "steps": steps, "final": "step budget exhausted"}
