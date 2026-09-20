"""Thin OpenAI-compatible model client for the minimal agent loop."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
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
        retry_base: float = 1.0,
        retry_max_wait: float = 60.0,
        timeout: float = 180.0,
    ) -> None:
        if not api_key:
            raise ModelError("empty API key")
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        self.model = model
        self.thinking = thinking
        self.temperature = temperature
        self.max_retries = max(1, int(max_retries))
        self.retry_base = max(0.05, float(retry_base))
        self.retry_max_wait = max(self.retry_base, float(retry_max_wait))

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> ModelReply:
        """Call the model, retrying transient provider/network failures.

        When ``deadline`` is supplied, transient failures are retried until the
        monotonic deadline rather than being limited to a fixed attempt count.
        Non-retryable request/auth errors still fail immediately.
        """

        last_error: Exception | None = None
        attempt = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelError(
                    f"model unavailable until deadline: {last_error or 'request not sent'}"
                )
            attempt += 1
            try:
                response = self.client.chat.completions.create(
                    **self._request_kwargs(messages, tools)
                )
                return self._parse_response(response)
            except (BadRequestError, AuthenticationError, PermissionDeniedError, NotFoundError):
                # Deterministic failures: retrying cannot help.
                raise
            except Exception as exc:  # noqa: BLE001 - provider exceptions vary
                last_error = exc
                if not self._is_retryable(exc):
                    raise ModelError(f"non-retryable model error: {exc}") from exc

                if deadline is None:
                    if attempt >= self.max_retries:
                        break
                    wait = self._backoff(attempt)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    wait = min(self._backoff(attempt), remaining)

                # Full jitter reduces retry storms across concurrent workers.
                wait *= random.uniform(0.5, 1.5)
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - time.monotonic()))
                if wait > 0:
                    time.sleep(wait)

        if deadline is not None:
            raise ModelError(f"model call unavailable before deadline: {last_error}")
        raise ModelError(f"model call failed after {attempt} attempts: {last_error}")

    def _request_kwargs(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
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
        return kwargs

    def _backoff(self, attempt: int) -> float:
        return min(
            self.retry_max_wait,
            self.retry_base * (2 ** max(0, attempt - 1)),
        )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        retryable = (
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            InternalServerError,
            ConnectionError,
            TimeoutError,
            OSError,
        )
        return isinstance(exc, retryable)

    @staticmethod
    def _parse_response(response: Any) -> ModelReply:
        message = response.choices[0].message
        payload = ModelClient._normalize_message(message)
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
