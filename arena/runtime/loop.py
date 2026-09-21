"""Minimal model-tool loop for one official challenge."""

from __future__ import annotations

import json
from typing import Any

from .events import EventLogger
from .model import ModelClient, ModelError
from .tools import TOOL_SCHEMAS, ToolExecutor

SYSTEM_PROMPT = """You are an autonomous CTF agent solving one authorized challenge.

You are given exactly one challenge: name, category, points, description, downloaded
files, and live connection info when present. Work only in the challenge work
directory for downloaded files and scratch scripts.

Use run_bash for concrete investigation and exploitation. Commands time out after
120 seconds. Use view_image to inspect local image or DICOM files when visual
analysis is useful. Use submit_flag when you have a candidate `INCYPHER{...}` flag.
If a submission is rejected, read the verdict and continue from the current context.
Do not fabricate flags and do not ask for human help.
"""

CONTINUATION = {
    "role": "user",
    "content": (
        "Continue working. Use a tool to make concrete progress. "
        "Only call submit_flag when you have a candidate flag."
    ),
}


def _estimate_text_tokens(text: Any) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    encoded = raw.encode("utf-8", errors="replace")
    return max(1, (len(encoded) + 2) // 3)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, list):
        # OpenAI-style multi-part messages may contain base64 data URLs.  Do
        # not estimate image bytes as text tokens; use a fixed vision cost.
        total = 16
        for part in content:
            if not isinstance(part, dict):
                total += _estimate_text_tokens(str(part))
                continue
            if part.get("type") == "image_url":
                total += 1024
            else:
                total += _estimate_text_tokens(part.get("text") or json.dumps(part, default=str))
        return total
    return _estimate_text_tokens(json.dumps(message, ensure_ascii=False, default=str))


def _preview(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "... "


class AgentRuntime:
    def __init__(
        self,
        *,
        model: Any,
        tools: ToolExecutor,
        max_steps: int = 40,
        context_window_tokens: int = 1_000_000,
        context_reserve_tokens: int = 8_000,
        max_total_tokens: int | None = None,
        max_plain_replies: int = 3,
        events: EventLogger | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max(1, int(max_steps))
        self.context_window_tokens = max(1, int(context_window_tokens))
        self.context_reserve_tokens = max(0, int(context_reserve_tokens))
        self.context_limit_tokens = max(
            1, self.context_window_tokens - self.context_reserve_tokens
        )
        if max_total_tokens is None or int(max_total_tokens) <= 0:
            self.max_total_tokens = self.context_window_tokens
        else:
            self.max_total_tokens = max(1, int(max_total_tokens))
        self.max_plain_replies = max(1, int(max_plain_replies))
        self.events = events or EventLogger(None)

    def _log(self, kind: str, **payload: Any) -> None:
        self.events.log(kind, **payload)

    def _estimate_next_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        baseline_tokens: int | None,
        baseline_message_count: int,
    ) -> int:
        if baseline_tokens is None:
            return sum(_estimate_message_tokens(message) for message in messages)
        new_messages = messages[baseline_message_count:]
        return baseline_tokens + sum(
            _estimate_message_tokens(message) for message in new_messages
        )

    def run(self, prompt: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user", "content": prompt},
        ]
        self._log(
            "run_start",
            prompt=prompt,
            max_steps=self.max_steps,
            context_window_tokens=self.context_window_tokens,
            context_reserve_tokens=self.context_reserve_tokens,
            context_limit_tokens=self.context_limit_tokens,
            max_total_tokens=self.max_total_tokens,
            max_plain_replies=self.max_plain_replies,
        )
        baseline_prompt_tokens: int | None = None
        baseline_message_count = 0
        cumulative_tokens = 0
        plain_reply_streak = 0

        for step in range(1, self.max_steps + 1):
            estimated_prompt_tokens = self._estimate_next_prompt_tokens(
                messages,
                baseline_tokens=baseline_prompt_tokens,
                baseline_message_count=baseline_message_count,
            )
            if estimated_prompt_tokens >= self.context_limit_tokens:
                result = {
                    "solved": False,
                    "steps": step - 1,
                    "error": "context_limit",
                    "context_limit_tokens": self.context_limit_tokens,
                    "estimated_prompt_tokens": estimated_prompt_tokens,
                }
                self._log("context_limit", **result)
                self._log("run_end", result=result)
                return result

            request_message_count = len(messages)
            self._log(
                "model_request",
                step=step,
                message_count=len(messages),
                last_message=_preview(messages[-1] if messages else None),
                estimated_prompt_tokens=estimated_prompt_tokens,
                context_limit_tokens=self.context_limit_tokens,
            )
            try:
                reply = self.model.chat(messages, TOOL_SCHEMAS)
            except ModelError as exc:
                result = {"solved": False, "steps": step, "error": f"ModelError: {exc}"}
                self._log("model_error", **result)
                self._log("run_end", result=result)
                return result
            except Exception as exc:  # provider-specific errors are reported, not raised
                result = {
                    "solved": False,
                    "steps": step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._log("model_error", **result)
                self._log("run_end", result=result)
                return result

            if reply.prompt_tokens > 0:
                baseline_prompt_tokens = reply.prompt_tokens
                baseline_message_count = request_message_count
            else:
                baseline_prompt_tokens = None

            assistant = reply.message
            tool_calls = assistant.get("tool_calls") or []
            usage_tokens = int(reply.total_tokens or 0)
            if usage_tokens <= 0:
                usage_tokens = int(reply.prompt_tokens or 0) + int(reply.completion_tokens or 0)
            cumulative_tokens += max(0, usage_tokens)
            self._log(
                "model_reply",
                step=step,
                message=assistant,
                content=assistant.get("content"),
                reasoning_content=assistant.get("reasoning_content"),
                tool_calls=tool_calls,
                usage={
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "total_tokens": reply.total_tokens,
                    "raw": reply.raw_usage,
                },
                estimated_prompt_tokens=estimated_prompt_tokens,
                cumulative_tokens=cumulative_tokens,
            )
            messages.append(assistant)

            if not tool_calls:
                plain_reply_streak += 1
                self._log("assistant_plain", step=step, content=assistant.get("content"))
                if cumulative_tokens >= self.max_total_tokens:
                    result = {
                        "solved": False,
                        "steps": step,
                        "error": "token_budget_exhausted",
                        "cumulative_tokens": cumulative_tokens,
                        "max_total_tokens": self.max_total_tokens,
                    }
                    self._log("token_budget_exhausted", **result)
                    self._log("run_end", result=result)
                    return result
                if plain_reply_streak >= self.max_plain_replies:
                    result = {
                        "solved": False,
                        "steps": step,
                        "error": "no_tool_progress",
                        "plain_reply_streak": plain_reply_streak,
                        "cumulative_tokens": cumulative_tokens,
                    }
                    self._log("no_tool_progress", **result)
                    self._log("run_end", result=result)
                    return result
                messages.append(dict(CONTINUATION))
                continue

            plain_reply_streak = 0
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = function.get("arguments") or "{}"
                call_id = str(call.get("id") or f"call_{step}")
                self._log(
                    "tool_call",
                    step=step,
                    tool_call_id=call_id,
                    name=name,
                    arguments=arguments,
                )
                outcome = self.tools.execute(name, arguments)
                self._log(
                    "tool_result",
                    step=step,
                    tool_call_id=call_id,
                    name=name,
                    content=outcome.content,
                    solved=outcome.solved,
                    flag=outcome.flag,
                    verdict=outcome.verdict,
                    image_count=len(outcome.images),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": outcome.content,
                    }
                )
                if outcome.images:
                    image_message = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Image attachment from view_image. "
                                    "Analyze the image and use it together with the rest of the challenge context."
                                ),
                            },
                            *[
                                {"type": "image_url", "image_url": {"url": data_url}}
                                for data_url in outcome.images
                            ],
                        ],
                    }
                    messages.append(image_message)
                if outcome.solved:
                    result = {
                        "solved": True,
                        "steps": step,
                        "flag": outcome.flag,
                        "verdict": outcome.verdict,
                    }
                    self._log("run_end", result=result)
                    return result

            if cumulative_tokens >= self.max_total_tokens:
                result = {
                    "solved": False,
                    "steps": step,
                    "error": "token_budget_exhausted",
                    "cumulative_tokens": cumulative_tokens,
                    "max_total_tokens": self.max_total_tokens,
                }
                self._log("token_budget_exhausted", **result)
                self._log("run_end", result=result)
                return result

        result = {
            "solved": False,
            "steps": self.max_steps,
            "error": "step_budget_exhausted",
        }
        self._log("step_budget_exhausted", result=result)
        self._log("run_end", result=result)
        return result
