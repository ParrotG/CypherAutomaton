"""Official Brain interface for the IN-CYPHER arena."""

from __future__ import annotations

import os
from typing import Any, Callable

from .runtime.events import EventLogger
from .runtime.loop import AgentRuntime
from .runtime.model import ModelClient
from .runtime.tools import ToolExecutor


class Brain:
    def __init__(
        self,
        run_bash: Callable[[str], str],
        submit_flag: Callable[[str], dict[str, Any]],
        max_steps: int = 40,
        *,
        model: Any | None = None,
        workspace_dir: str | None = None,
        events_path: str | None = None,
        log_context: dict[str, Any] | None = None,
    ) -> None:
        self.run_bash = run_bash
        self.submit_flag = submit_flag
        self.max_steps = int(os.environ.get("MAX_STEPS", max_steps))
        self.model = model
        self.workspace_dir = workspace_dir
        self.events_path = events_path
        self.log_context = dict(log_context or {})
        self.context_window_tokens = int(
            os.environ.get("CONTEXT_WINDOW_TOKENS", "1000000")
        )
        self.context_reserve_tokens = int(
            os.environ.get("CONTEXT_RESERVE_TOKENS", "8000")
        )

    def solve(self, prompt: str) -> dict[str, Any]:
        model = self.model or ModelClient.from_env()
        tools = ToolExecutor(
            run_bash=self.run_bash,
            submit_flag=self.submit_flag,
            workspace_dir=self.workspace_dir,
            max_output=int(os.environ.get("MAX_TOOL_OUTPUT", "20000")),
        )
        events = EventLogger(self.events_path, base=self.log_context)
        try:
            return AgentRuntime(
                model=model,
                tools=tools,
                max_steps=self.max_steps,
                context_window_tokens=self.context_window_tokens,
                context_reserve_tokens=self.context_reserve_tokens,
                events=events,
            ).run(prompt)
        finally:
            events.close()
