"""Prompt construction for the minimal agent unit."""

from __future__ import annotations

from .config import AgentConfig


SYSTEM_PROMPT = """You are an autonomous CTF agent working inside an isolated workspace.

Rules:
- Work only in the provided workspace.
- Use tools for every action; do not merely describe what you would do.
- You may write and run scripts, inspect files, and connect to the challenge service through the local endpoint.
- The bash tool runs in a sandbox. It can see the workspace at /workspace and a preinstalled Python tool environment; it cannot read the host project or secrets.
- If you need additional Python packages, create a venv inside /workspace with `python -m virtualenv .venv` and install them with `.venv/bin/pip`.
- The platform submission format is always `INCYPHER{...}`. Source writeups may use `flag{...}` or another wrapper; report the raw body or source candidate and the harness will normalize it.
- After `report_flag`, the run waits for verification. If a candidate is rejected, the rejection feedback will be injected and you should continue from the same context.
- After a meaningful unit of work concludes, briefly record any outcome that may be useful to other agents. Do not log routine intermediate steps.
- Continue working until you have a flag and call report_flag.
- Do not ask the human for help.
- Keep commands focused and verify your reasoning with actual tool output.
"""


def build_initial_messages(config: AgentConfig) -> list[dict[str, str]]:
    """Return the stable initial conversation."""

    description = (config.description_text or "").strip()
    if not description and config.challenge_md:
        description = "(description file exists; read it with the editor or bash)"
    if not description:
        description = "(no challenge description provided)"

    endpoint = config.agent_endpoint or "(direct task, no bridge endpoint)"
    blackboard_line = (
        f"Blackboard: {config.blackboard_path} (bb-read / bb-write)\n"
        if config.blackboard_dir
        else ""
    )
    user = f"""Challenge: {config.challenge_id}
Run: {config.run_id}
Name: {config.challenge_name or config.challenge_id}
Category: {config.category or "unknown"}
Target: {config.target or "(not provided)"}
Local agent endpoint: {endpoint}
Sandbox workspace: /workspace
{blackboard_line}Submission format: `INCYPHER{{...}}` (the harness normalizes raw or source-wrapped candidates).
Flag pattern: {config.flag_pattern}
Use `report_flag` when you have a candidate; the run will wait for manual verification.

Challenge description:
{description}

Begin now. Use the tools to make progress."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT.strip()},
        {"role": "user", "content": user.strip()},
    ]


def continuation_message() -> dict[str, str]:
    """Nudge the model back into tool use after a plain assistant message."""

    return {
        "role": "user",
        "content": (
            "Continue working. Use a tool to make concrete progress. "
            "Only call report_flag when you have a candidate flag."
        ),
    }
