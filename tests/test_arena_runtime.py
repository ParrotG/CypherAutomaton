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

    def test_context_limit_stops_before_next_model_call(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    message={"role": "assistant", "content": "thinking"},
                    prompt_tokens=5950,
                )
            ]
        )
        with patch.dict(
            os.environ,
            {"CONTEXT_WINDOW_TOKENS": "6000", "CONTEXT_RESERVE_TOKENS": "100"},
            clear=False,
        ):
            brain = Brain(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {},
                max_steps=100,
                model=model,
            )
            result = brain.solve("solve this")
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "context_limit")

    def test_events_jsonl_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events = Path(temp) / "events.jsonl"
            model = ScriptedModel(
                [tool_reply("c1", "submit_flag", {"flag": "INCYPHER{ok}"})]
            )
            brain = Brain(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {"status": "correct"},
                max_steps=3,
                model=model,
                events_path=str(events),
            )
            result = brain.solve("solve this")
            lines = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
        self.assertTrue(result["solved"])
        kinds = [line["kind"] for line in lines]
        model_requests = [line for line in lines if line["kind"] == "model_request"]
        self.assertTrue(model_requests)
        self.assertNotIn("messages", model_requests[0])
        self.assertIn("message_count", model_requests[0])
        for expected in (
            "run_start",
            "model_request",
            "model_reply",
            "tool_call",
            "tool_result",
            "run_end",
        ):
            self.assertIn(expected, kinds)

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



    def test_explicit_max_steps_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"MAX_STEPS": "7"}, clear=False):
            brain = Brain(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {},
                max_steps=123,
            )
        self.assertEqual(brain.max_steps, 123)

    def test_token_budget_stops_attempt(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    message={"role": "assistant", "content": "thinking"},
                    total_tokens=100,
                )
            ]
        )
        with patch.dict(
            os.environ,
            {"MAX_ATTEMPT_TOKENS": "50", "MAX_PLAIN_REPLIES": "99"},
            clear=False,
        ):
            brain = Brain(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {},
                max_steps=10,
                model=model,
            )
            result = brain.solve("solve this")
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "token_budget_exhausted")

    def test_no_tool_progress_stops_plain_streak(self) -> None:
        model = ScriptedModel([plain_reply("a"), plain_reply("b")])
        with patch.dict(
            os.environ,
            {"MAX_PLAIN_REPLIES": "2", "MAX_ATTEMPT_TOKENS": "1000000"},
            clear=False,
        ):
            brain = Brain(
                run_bash=lambda _cmd: "",
                submit_flag=lambda _flag: {},
                max_steps=10,
                model=model,
            )
            result = brain.solve("solve this")
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "no_tool_progress")

class ArenaModelTests(unittest.TestCase):

    def test_provider_defaults_openrouter(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "test-key",
            },
            clear=True,
        ):
            client = ModelClient.from_env()
        self.assertEqual(client.provider, "openrouter")
        self.assertEqual(client.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(client.model, "deepseek/deepseek-v4.1-flash")

    def test_provider_defaults_deepseek(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-key",
            },
            clear=True,
        ):
            client = ModelClient.from_env()
        self.assertEqual(client.provider, "deepseek")
        self.assertEqual(client.base_url, "https://api.deepseek.com")
        self.assertEqual(client.model, "deepseek-flash")

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


    def test_openrouter_request_body_sets_reasoning_and_provider(self) -> None:
        client = ModelClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="deepseek/deepseek-v4.1-flash",
        )
        kwargs = client._request_kwargs([{"role": "user", "content": "hi"}], [])
        self.assertEqual(kwargs["extra_body"]["reasoning"], {"effort": "high"})
        self.assertEqual(
            kwargs["extra_body"]["provider"],
            {"order": ["deepseek"], "allow_fallbacks": True},
        )

    def test_deepseek_request_body_has_no_openrouter_settings(self) -> None:
        client = ModelClient(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
        )
        kwargs = client._request_kwargs([{"role": "user", "content": "hi"}], [])
        self.assertNotIn("extra_body", kwargs)

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
