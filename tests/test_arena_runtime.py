from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.brain import Brain
from arena.runtime.model import ModelClient, ModelConfigError, ModelReply
from arena.runtime.tools import ToolExecutor


class ScriptedModel:
    def __init__(self, script: list[ModelReply]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools, *, deadline=None):
        self.calls += 1
        if not self.script:
            raise AssertionError("scripted model exhausted")
        return self.script.pop(0)


def tool_reply(call_id: str, name: str, arguments: dict) -> ModelReply:
    return ModelReply(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
    )


def plain_reply(content: str = "thinking") -> ModelReply:
    return ModelReply(message={"role": "assistant", "content": content})


class ArenaBrainTests(unittest.TestCase):
    def test_success_via_submit_flag(self) -> None:
        submitted: list[str] = []

        def submit(flag: str) -> dict:
            submitted.append(flag)
            return {"status": "correct"}

        with tempfile.TemporaryDirectory() as temp:
            model = ScriptedModel(
                [
                    tool_reply("c1", "run_bash", {"command": "echo hi"}),
                    tool_reply("c2", "submit_flag", {"flag": "flag{abc}"}),
                ]
            )
            brain = Brain(
                run_bash=lambda _cmd: "hi",
                submit_flag=submit,
                max_steps=5,
                model=model,
                workspace_dir=temp,
            )
            result = brain.solve("solve this")
        self.assertTrue(result["solved"])
        self.assertEqual(result["flag"], "INCYPHER{abc}")
        self.assertEqual(submitted, ["INCYPHER{abc}"])

    def test_rejected_flag_continues_then_succeeds(self) -> None:
        calls = 0

        def submit(_flag: str) -> dict:
            nonlocal calls
            calls += 1
            return {"status": "incorrect"} if calls == 1 else {"status": "already_solved"}

        model = ScriptedModel(
            [
                tool_reply("c1", "submit_flag", {"flag": "INCYPHER{bad}"}),
                tool_reply("c2", "submit_flag", {"flag": "INCYPHER{good}"}),
            ]
        )
        brain = Brain(run_bash=lambda _cmd: "", submit_flag=submit, max_steps=5, model=model)
        result = brain.solve("solve this")
        self.assertTrue(result["solved"])
        self.assertEqual(result["flag"], "INCYPHER{good}")
        self.assertEqual(calls, 2)

    def test_step_budget_exhausted(self) -> None:
        model = ScriptedModel([plain_reply(), plain_reply()])
        brain = Brain(run_bash=lambda _cmd: "", submit_flag=lambda _flag: {}, max_steps=2, model=model)
        result = brain.solve("solve this")
        self.assertFalse(result["solved"])
        self.assertEqual(result["steps"], 2)

    def test_editor_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executor = ToolExecutor(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {},
                workspace_dir=temp,
            )
            outcome = executor.execute(
                "str_replace_editor", {"command": "view", "path": "../outside.txt"}
            )
        self.assertIn("escapes workspace", outcome.content)

    def test_normalizes_raw_flag(self) -> None:
        executor = ToolExecutor(
            run_bash=lambda _cmd: "",
            submit_flag=lambda _flag: {"status": "correct"},
        )
        outcome = executor.execute("submit_flag", {"flag": "abc123"})
        self.assertTrue(outcome.solved)
        self.assertEqual(outcome.flag, "INCYPHER{abc123}")


class ArenaModelTests(unittest.TestCase):
    def test_from_env_requires_key(self) -> None:
        with patch.dict(os.environ, {"LLM_MODEL": "some-model"}, clear=False):
            os.environ.pop("LLM_API_KEY", None)
            with self.assertRaises(ModelConfigError):
                ModelClient.from_env()

    def test_from_env_reads_official_variables(self) -> None:
        env = {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://example.invalid/v1",
            "LLM_MODEL": "test-model",
        }
        with patch.dict(os.environ, env, clear=False):
            client = ModelClient.from_env()
        self.assertEqual(client.model, "test-model")

    def test_transient_connection_errors_are_retried(self) -> None:
        client = ModelClient(
            api_key="test",
            base_url="https://example.invalid/v1",
            model="test-model",
            max_retries=3,
            retry_base=0.001,
            retry_max_wait=0.002,
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        model_dump=lambda exclude_none=False: {
                            "role": "assistant",
                            "content": "OK",
                        }
                    )
                )
            ],
            usage=None,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[ConnectionError("one"), ConnectionError("two"), response],
        ):
            reply = client.chat([{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(reply.message["content"], "OK")


if __name__ == "__main__":
    unittest.main()
