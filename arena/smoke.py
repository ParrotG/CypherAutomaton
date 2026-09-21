"""Minimal live smoke test for the configured OpenAI-compatible LLM.

This performs one real /chat/completions request and never touches the CTF
platform. It is intended for OpenRouter / DeepSeek / any OpenAI-compatible
endpoint configured through the official LLM_* variables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .runtime.model import ModelClient, ModelConfigError, ModelError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arena-smoke")
    parser.add_argument("--prompt", default="Reply with exactly: OK")
    args = parser.parse_args(argv)

    try:
        client = ModelClient.from_env()
    except ModelConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    messages = [
        {"role": "system", "content": "You are a connectivity smoke test."},
        {"role": "user", "content": args.prompt},
    ]
    try:
        reply = client.chat(messages, tools=[])
    except ModelError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - smoke reports provider errors
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    content = str(reply.message.get("content") or "").strip()
    result = {
        "ok": True,
        "provider": getattr(client, "provider", "custom"),
        "base_url": getattr(client, "base_url", os.environ.get("LLM_BASE_URL", "")),
        "model": getattr(client, "model", os.environ.get("LLM_MODEL", "")),
        "reply": content[:200],
        "total_tokens": reply.total_tokens,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
