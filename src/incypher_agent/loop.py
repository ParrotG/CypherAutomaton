"""Minimal long-running model-tool loop for one CTF challenge."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .model import ModelClient, ModelError
from .prompt import build_initial_messages, continuation_message
from .state import AgentResult, AgentStore
from .tools import TOOL_SCHEMAS, ToolExecutor


def _short(text: Any, limit: int = 500) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


class AgentLoop:
    """A small outer loop that calls a model and executes tool calls.

    In this phase the only automatic stop conditions are hard limits and a
    successful ``report_flag`` call.  A plain assistant message does not stop
    the run; the loop nudges the model to continue using tools.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        model: Any | None = None,
        store: AgentStore | None = None,
        tools: ToolExecutor | None = None,
    ) -> None:
        self.config = config
        self.store = store or AgentStore(config.run_dir, verbose=config.verbose)
        self.tools = tools or ToolExecutor(config)
        self.model = model or ModelClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            thinking=config.thinking,
            temperature=config.temperature,
            timeout=180.0,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> AgentResult:
        started = time.monotonic()
        model_calls = 0
        tool_calls = 0
        prompt_tokens = 0
        completion_tokens = 0

        self.store.set_state(
            status="running",
            model=self.config.model,
            challenge_id=self.config.challenge_id,
            run_id=self.config.run_id,
            agent_endpoint=self.config.agent_endpoint,
            workspace=str(self.config.workspace_dir),
        )
        self.store.event(
            "start",
            {
                "challenge_id": self.config.challenge_id,
                "run_id": self.config.run_id,
                "model": self.config.model,
                "base_url": self.config.base_url,
                "workspace": str(self.config.workspace_dir),
                "agent_endpoint": self.config.agent_endpoint,
                "max_seconds": self.config.max_seconds,
                "max_model_calls": self.config.max_model_calls,
                "max_tool_calls": self.config.max_tool_calls,
            },
        )
        messages = build_initial_messages(self.config)

        try:
            while True:
                limit_reason = self._hard_limit_reason(
                    started=started,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                )
                if limit_reason:
                    return self._finish_hard_limit(limit_reason, model_calls, tool_calls)

                self.store.heartbeat(
                    f"model_call={model_calls} tool_calls={tool_calls}"
                )
                self.store.event(
                    "model_request",
                    {
                        "model_calls": model_calls + 1,
                        "message_count": len(messages),
                        "last_message": _short(messages[-1].get("content"), 300),
                    },
                )

                try:
                    reply = self.model.chat(messages, TOOL_SCHEMAS)
                except ModelError as exc:
                    return self._finish_model_error(str(exc), model_calls, tool_calls)

                model_calls += 1
                prompt_tokens += reply.prompt_tokens
                completion_tokens += reply.completion_tokens
                total_tokens = prompt_tokens + completion_tokens
                assistant_message = reply.message
                tool_calls_payload = assistant_message.get("tool_calls") or []

                self.store.set_state(
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
                self.store.event(
                    "model_reply",
                    {
                        "model_calls": model_calls,
                        "content": _short(assistant_message.get("content"), 1000),
                        "reasoning_content": _short(
                            assistant_message.get("reasoning_content"), 1500
                        ),
                        "tool_calls": [
                            {
                                "id": call.get("id"),
                                "name": (call.get("function") or {}).get("name"),
                                "arguments": _short(
                                    (call.get("function") or {}).get("arguments"), 800
                                ),
                            }
                            for call in tool_calls_payload
                        ],
                        "usage": {
                            "prompt_tokens": reply.prompt_tokens,
                            "completion_tokens": reply.completion_tokens,
                            "total_tokens": reply.total_tokens,
                        },
                    },
                )
                messages.append(assistant_message)

                if tool_calls_payload:
                    for call in tool_calls_payload:
                        limit_reason = self._hard_limit_reason(
                            started=started,
                            model_calls=model_calls,
                            tool_calls=tool_calls,
                        )
                        if limit_reason:
                            return self._finish_hard_limit(limit_reason, model_calls, tool_calls)

                        call_id = str(call.get("id") or f"call_{tool_calls}")
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        arguments = function.get("arguments") or "{}"
                        self.store.event(
                            "tool_call",
                            {
                                "tool_call_id": call_id,
                                "name": name,
                                "arguments": _short(arguments, 1500),
                            },
                        )
                        outcome = self.tools.execute(name, arguments, tool_call_id=call_id)
                        tool_calls += 1
                        self.store.set_state(tool_calls=tool_calls)
                        self.store.event(
                            "tool_result",
                            {
                                "tool_call_id": call_id,
                                "name": name,
                                "done": outcome.done,
                                "content": _short(outcome.content, 2000),
                            },
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": outcome.content,
                            }
                        )
                        if outcome.done and outcome.flag:
                            self.store.write_flag(
                                outcome.flag,
                                evidence=_short(outcome.content, 1000),
                            )
                            self.store.event(
                                "success",
                                {"flag": outcome.flag, "status": outcome.status},
                            )
                            self.store.set_state(
                                status="SUCCESS",
                                flag=outcome.flag,
                                stop_reason="flag_reported",
                                exit_code=0,
                            )
                            self.store.close()
                            return AgentResult(
                                status="SUCCESS",
                                flag=outcome.flag,
                                reason="flag_reported",
                                exit_code=0,
                            )
                    continue

                # A plain assistant message is treated as a planning/turn note,
                # not as the end of the run.  Continue with a nudge.
                self.store.event(
                    "assistant_plain",
                    {"content": _short(assistant_message.get("content"), 1500)},
                )
                messages.append(continuation_message())
        except KeyboardInterrupt:
            self.store.set_state(
                status="STOPPED",
                stop_reason="keyboard_interrupt",
                exit_code=130,
            )
            self.store.close()
            return AgentResult(status="STOPPED", reason="keyboard_interrupt", exit_code=130)

    # ------------------------------------------------------------------
    # Hard limits / failure handling
    # ------------------------------------------------------------------
    def _hard_limit_reason(
        self,
        *,
        started: float,
        model_calls: int,
        tool_calls: int,
    ) -> str | None:
        elapsed = time.monotonic() - started
        if elapsed >= self.config.max_seconds:
            return f"max_seconds exceeded ({elapsed:.1f}s >= {self.config.max_seconds:.1f}s)"
        if model_calls >= self.config.max_model_calls:
            return f"max_model_calls reached ({model_calls})"
        if tool_calls >= self.config.max_tool_calls:
            return f"max_tool_calls reached ({tool_calls})"
        return None

    def _finish_hard_limit(
        self,
        reason: str,
        model_calls: int,
        tool_calls: int,
    ) -> AgentResult:
        payload = {
            "reason": "hard_limit",
            "detail": reason,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "workspace": str(self.config.workspace_dir),
            "recorded_at": self.store.state.updated_at,
        }
        self.store.event("hard_limit", payload)
        self.store.write_escalation(payload)
        self.store.set_state(
            status="HARD_LIMIT",
            stop_reason=reason,
            exit_code=10,
        )
        self.store.close()
        return AgentResult(status="HARD_LIMIT", reason=reason, exit_code=10)

    def _finish_model_error(
        self,
        detail: str,
        model_calls: int,
        tool_calls: int,
    ) -> AgentResult:
        payload = {
            "reason": "model_error",
            "detail": detail,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "workspace": str(self.config.workspace_dir),
        }
        self.store.event("model_error", payload)
        self.store.write_escalation(payload)
        self.store.set_state(
            status="MODEL_ERROR",
            last_error=detail,
            stop_reason="model_error",
            exit_code=50,
        )
        self.store.close()
        return AgentResult(status="MODEL_ERROR", reason=detail, exit_code=50)


def read_agent_state(run_dir: Path) -> dict[str, Any]:
    state_file = Path(run_dir) / "agent" / "state.json"
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
