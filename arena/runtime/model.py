"""OpenAI-compatible model client for the arena brain.

The official runtime injects ``LLM_API_KEY``, ``LLM_BASE_URL`` and
``LLM_MODEL``.  This client reads those variables directly; there is no
legacy DeepSeek fallback layer.
"""

from __future__ import annotations

import os
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

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"


class ModelConfigError(RuntimeError):
    """Raised when LLM_* environment variables are missing or invalid."""


class ModelError(RuntimeError):
    """Raised when a model call fails after retries."""


@dataclass
class ModelReply:
    message: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)


class ModelClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = 2048,
        timeout: float = 180.0,
        max_retries: int = 3,
        retry_base: float = 1.0,
        retry_max_wait: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ModelConfigError("LLM_API_KEY is required")
        if not base_url or not base_url.strip():
            raise ModelConfigError("LLM_BASE_URL is required")
        if not model or not model.strip():
            raise ModelConfigError("LLM_MODEL is required")

        headers: dict[str, str] = {}
        if "openrouter.ai" in base_url:
            headers.update(
                {
                    "HTTP-Referer": os.environ.get(
                        "LLM_HTTP_REFERER", "https://in-cypher.com"
                    ),
                    "X-Title": os.environ.get("LLM_X_TITLE", "IN-CYPHER Arena"),
                }
            )
        if extra_headers:
            headers.update(extra_headers)

        self.base_url = base_url.rstrip("/")
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
            default_headers=headers or None,
        )
        self.model = model.strip()
        self.provider = "custom"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max(1, int(max_retries))
        self.retry_base = max(0.05, float(retry_base))
        self.retry_max_wait = max(self.retry_base, float(retry_max_wait))

    @classmethod
    def from_env(cls) -> "ModelClient":
        explicit_key = os.environ.get("LLM_API_KEY", "").strip()
        explicit_base = os.environ.get("LLM_BASE_URL", "").strip()
        explicit_model = os.environ.get("LLM_MODEL", "").strip()
        provider = (os.environ.get("LLM_PROVIDER", "deepseek").strip().lower() or "deepseek")
        if provider == "openrouter":
            key = explicit_key or os.environ.get("OPENROUTER_API_KEY", "").strip()
            base_url = (
                explicit_base
                or os.environ.get("OPENROUTER_BASE_URL", "").strip()
                or DEFAULT_OPENROUTER_BASE_URL
            )
            model = (
                explicit_model
                or os.environ.get("OPENROUTER_MODEL", "").strip()
                or DEFAULT_OPENROUTER_MODEL
            )
        elif provider == "deepseek":
            key = explicit_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
            base_url = (
                explicit_base
                or os.environ.get("DEEPSEEK_BASE_URL", "").strip()
                or DEFAULT_DEEPSEEK_BASE_URL
            )
            model = (
                explicit_model
                or os.environ.get("DEEPSEEK_MODEL", "").strip()
                or DEFAULT_DEEPSEEK_MODEL
            )
        else:
            raise ModelConfigError("LLM_PROVIDER must be deepseek or openrouter")
        client = cls(api_key=key, base_url=base_url, model=model)
        client.provider = provider
        return client

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> ModelReply:
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
                raise
            except Exception as exc:  # provider exceptions vary
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
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs

    def _backoff(self, attempt: int) -> float:
        return min(self.retry_max_wait, self.retry_base * (2 ** max(0, attempt - 1)))

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
