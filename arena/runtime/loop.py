"""Minimal model-tool loop for one official challenge."""

from __future__ import annotations

import json
from typing import Any

from .model import ModelClient, ModelError
from .tools import TOOL_SCHEMAS, ToolExecutor

SYSTEM_PROMPT = """You are an autonomous CTF agent solving one authorized challenge.

You are given exactly one challenge: name, category, points, description, downloaded
files, and live connection info when present. Work only in the challenge work
directory for downloaded files and scratch scripts.

Use run_bash for concrete investigation and exploitation. Commands time out after
120 seconds. Use submit_flag when you have a candidate `INCYPHER{...}` flag. If a
submission is rejected, read the verdict and continue from the current context.
Do not fabricate flags and do not ask for human help.
"""

CONTINUATION = {
    "role": "user",
    "content": (
        "Continue working. Use a tool to make concrete progress. "
        "Only call submit_flag when you have a candidate flag."
    ),
}


def _short(text: Any, limit: int = 500) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "..."


class AgentRuntime:
    def __init__(
        self,
        *,
        model: Any,
        tools: ToolExecutor,
        max_steps: int = 40,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max(1, int(max_steps))

    def run(self, prompt: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user", "content": prompt},
        ]
        for step in range(1, self.max_steps + 1):
            try:
                reply = self.model.chat(messages, TOOL_SCHEMAS)
            except ModelError as exc:
                return {"solved": False, "steps": step, "error": f"ModelError: {exc}"}
            except Exception as exc:  # provider-specific errors are reported, not raised
                return {"solved": False, "steps": step, "error": f"{type(exc).__name__}: {exc}"}

            assistant = reply.message
            tool_calls = assistant.get("tool_calls") or []
            messages.append(assistant)
            if not tool_calls:
                text = str(assistant.get("content") or "").strip()
                if text:
                    messages.append(dict(CONTINUATION))
                else:
                    messages.append(dict(CONTINUATION))
                continue

            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = function.get("arguments") or "{}"
                outcome = self.tools.execute(name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or f"call_{step}"),
                        "content": outcome.content,
                    }
                )
                if outcome.solved:
                    return {
                        "solved": True,
                        "steps": step,
                        "flag": outcome.flag,
                        "verdict": outcome.verdict,
                    }
        return {"solved": False, "steps": self.max_steps, "final": "step budget exhausted"}
