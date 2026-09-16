"""Thin OpenAI-compatible model client for the minimal agent loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)


@dataclass
class ModelReply:
    message: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)


class ModelError(RuntimeError):
    """Raised when a model call fails after retries."""


class ModelClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str | None = None,
        temperature: float | None = None,
        max_retries: int = 3,
        timeout: float = 180.0,
    ) -> None:
        if not api_key:
            raise ModelError("empty API key")
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        self.model = model
        self.thinking = thinking
        self.temperature = temperature
        self.max_retries = max(1, int(max_retries))

    def chat(self, messages: Sequence[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": list(messages),
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if self.temperature is not None:
                    kwargs["temperature"] = self.temperature
                if self.thinking:
                    value = self.thinking.strip().lower()
                    if value in {"enabled", "disabled"}:
                        kwargs["extra_body"] = {"thinking": {"type": value}}
                    elif value in {"1", "true", "on"}:
                        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    elif value in {"0", "false", "off"}:
                        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

                response = self.client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                payload = self._normalize_message(message)
                usage = getattr(response, "usage", None)
                reply = ModelReply(message=payload)
                if usage is not None:
                    reply.prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    reply.completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    reply.total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
                    try:
                        reply.raw_usage = usage.model_dump()
                    except Exception:
                        reply.raw_usage = {}
                return reply
            except BadRequestError:
                # Request-format errors are deterministic; do not retry.
                raise
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(10.0, 1.5 ** attempt))
            except Exception as exc:  # noqa: BLE001 - surface provider-specific errors after retry
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(10.0, 1.5 ** attempt))
        raise ModelError(f"model call failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _normalize_message(message: Any) -> dict[str, Any]:
        try:
            payload = message.model_dump(exclude_none=True)
        except Exception:
            payload = dict(message)
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning and "reasoning_content" not in payload:
            payload["reasoning_content"] = reasoning
        # Ensure tool_calls are plain dicts for JSON/session handling.
        tool_calls = payload.get("tool_calls")
        if tool_calls:
            normalized = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    normalized.append(tool_call)
                else:
                    normalized.append(tool_call.model_dump(exclude_none=True))
            payload["tool_calls"] = normalized
        payload.setdefault("role", "assistant")
        return payload
