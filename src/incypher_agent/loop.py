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


def _estimate_text_tokens(text: Any) -> int:
    """Conservative token estimate for text without a provider tokenizer."""

    raw = str(text or "")
    if not raw:
        return 0
    encoded = raw.encode("utf-8", errors="replace")
    return max(1, (len(encoded) + 2) // 3)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    return _estimate_text_tokens(
        json.dumps(message, ensure_ascii=False, default=str)
    )


class AgentLoop:
    """A small outer loop that calls a model and executes tool calls.

    Automatic stop conditions are the wall-clock limit, the context-window
    budget, and a successful ``report_flag`` call.  A plain assistant message
    does not stop the run; the loop nudges the model to continue using tools.
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
            retry_base=config.model_retry_base,
            retry_max_wait=config.model_retry_max_wait,
            timeout=180.0,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> AgentResult:
        started = time.monotonic()
        deadline = started + self.config.max_seconds
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
                "context_window_tokens": self.config.context_window_tokens,
                "context_reserve_tokens": self.config.context_reserve_tokens,
            },
        )
        messages = build_initial_messages(self.config)
        context_limit_tokens = (
            self.config.context_window_tokens - self.config.context_reserve_tokens
        )
        last_prompt_tokens: int | None = None
        last_prompt_message_count = 0

        try:
            while True:
                time_reason = self._time_limit_reason(started=started)
                if time_reason:
                    return self._finish_time_limit(time_reason, model_calls, tool_calls)

                estimated_prompt_tokens = self._estimate_next_prompt_tokens(
                    messages,
                    baseline_tokens=last_prompt_tokens,
                    baseline_message_count=last_prompt_message_count,
                )
                if estimated_prompt_tokens >= context_limit_tokens:
                    return self._finish_context_limit(
                        estimated_prompt_tokens=estimated_prompt_tokens,
                        last_prompt_tokens=last_prompt_tokens,
                        model_calls=model_calls,
                        tool_calls=tool_calls,
                    )

                self.store.heartbeat(
                    f"model_call={model_calls} tool_calls={tool_calls} "
                    f"estimated_context={estimated_prompt_tokens}"
                )
                self.store.event(
                    "model_request",
                    {
                        "model_calls": model_calls + 1,
                        "message_count": len(messages),
                        "estimated_prompt_tokens": estimated_prompt_tokens,
                        "context_limit_tokens": context_limit_tokens,
                        "last_message": _short(messages[-1].get("content"), 300),
                    },
                )

                request_message_count = len(messages)
                try:
                    reply = self.model.chat(
                        messages,
                        TOOL_SCHEMAS,
                        deadline=deadline,
                    )
                except ModelError as exc:
                    if time.monotonic() >= deadline:
                        return self._finish_time_limit(
                            f"model unavailable until max_seconds: {exc}",
                            model_calls,
                            tool_calls,
                        )
                    return self._finish_model_error(str(exc), model_calls, tool_calls)

                if reply.prompt_tokens > 0:
                    last_prompt_tokens = reply.prompt_tokens
                    last_prompt_message_count = request_message_count
                else:
                    last_prompt_tokens = None

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
                        "context": {
                            "estimated_prompt_tokens": estimated_prompt_tokens,
                            "context_limit_tokens": context_limit_tokens,
                        },
                    },
                )
                messages.append(assistant_message)

                if tool_calls_payload:
                    for call in tool_calls_payload:
                        time_reason = self._time_limit_reason(started=started)
                        if time_reason:
                            return self._finish_time_limit(
                                time_reason, model_calls, tool_calls
                            )

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
                            candidate = {
                                "flag": outcome.flag,
                                "raw_flag": outcome.raw_flag,
                                "flag_body": outcome.flag_body,
                                "evidence": _short(outcome.content, 1500),
                            }
                            self.store.write_candidate(candidate)
                            self.store.event("candidate_reported", candidate)
                            self.store.set_state(
                                status="WAITING_VERIFICATION",
                                flag=outcome.flag,
                                stop_reason="candidate_reported",
                            )
                            verification = self._wait_for_verification(deadline=deadline)
                            if verification is None:
                                return self._finish_time_limit(
                                    "verification wait exceeded max_seconds",
                                    model_calls,
                                    tool_calls,
                                )
                            if verification.get("success"):
                                self.store.write_flag(
                                    outcome.flag,
                                    evidence=_short(outcome.content, 1000),
                                )
                                self.store.event(
                                    "success",
                                    {"flag": outcome.flag, "status": "FLAG_VERIFIED"},
                                )
                                self.store.set_state(
                                    status="SUCCESS",
                                    flag=outcome.flag,
                                    stop_reason="flag_verified",
                                    exit_code=0,
                                )
                                self.store.close()
                                return AgentResult(
                                    status="SUCCESS",
                                    flag=outcome.flag,
                                    reason="flag_verified",
                                    exit_code=0,
                                )
                            feedback = str(
                                verification.get("feedback")
                                or "Candidate rejected by verification."
                            )
                            self.store.event(
                                "verification_failed",
                                {"flag": outcome.flag, "feedback": feedback},
                            )
                            self.store.set_state(status="running", stop_reason=None)
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        f"Platform rejected candidate {outcome.flag}. "
                                        f"Feedback: {feedback}\n"
                                        "Do not report the same candidate again. "
                                        "Continue working from the current context."
                                    ),
                                }
                            )
                            break
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
    # Verification wait / limits
    # ------------------------------------------------------------------
    def _wait_for_verification(self, *, deadline: float) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            verification = self.store.consume_verification()
            if verification is not None:
                return verification
            self.store.heartbeat("waiting_verification")
            time.sleep(self.config.verification_poll_interval)
        return None

    def _time_limit_reason(self, *, started: float) -> str | None:
        elapsed = time.monotonic() - started
        if elapsed >= self.config.max_seconds:
            return f"max_seconds exceeded ({elapsed:.1f}s >= {self.config.max_seconds:.1f}s)"
        return None

    def _estimate_next_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        baseline_tokens: int | None,
        baseline_message_count: int,
    ) -> int:
        """Estimate the next API prompt size.

        ``baseline_tokens`` is the actual prompt size reported by the previous
        API call.  Messages appended after that call are estimated locally
        until the next API call provides an exact usage figure again.
        """

        if baseline_tokens is None:
            return sum(_estimate_message_tokens(message) for message in messages)
        new_messages = messages[baseline_message_count:]
        return baseline_tokens + sum(
            _estimate_message_tokens(message) for message in new_messages
        )

    def _finish_time_limit(
        self,
        reason: str,
        model_calls: int,
        tool_calls: int,
    ) -> AgentResult:
        payload = {
            "reason": "time_limit",
            "detail": reason,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "workspace": str(self.config.workspace_dir),
            "recorded_at": self.store.state.updated_at,
        }
        self.store.event("time_limit", payload)
        self.store.write_escalation(payload)
        self.store.set_state(
            status="TIME_LIMIT",
            stop_reason="time_limit",
            exit_code=10,
        )
        self.store.close()
        return AgentResult(status="TIME_LIMIT", reason="time_limit", exit_code=10)

    def _finish_context_limit(
        self,
        *,
        estimated_prompt_tokens: int,
        last_prompt_tokens: int | None,
        model_calls: int,
        tool_calls: int,
    ) -> AgentResult:
        context_limit_tokens = (
            self.config.context_window_tokens - self.config.context_reserve_tokens
        )
        payload = {
            "reason": "context_limit",
            "detail": (
                f"estimated next prompt {estimated_prompt_tokens} tokens "
                f">= limit {context_limit_tokens}"
            ),
            "context_window_tokens": self.config.context_window_tokens,
            "context_reserve_tokens": self.config.context_reserve_tokens,
            "context_limit_tokens": context_limit_tokens,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "last_prompt_tokens": last_prompt_tokens,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "workspace": str(self.config.workspace_dir),
            "recorded_at": self.store.state.updated_at,
        }
        self.store.event("context_limit", payload)
        self.store.write_escalation(payload)
        self.store.set_state(
            status="CONTEXT_LIMIT",
            stop_reason="context_limit",
            exit_code=10,
        )
        self.store.close()
        return AgentResult(status="CONTEXT_LIMIT", reason="context_limit", exit_code=10)

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
