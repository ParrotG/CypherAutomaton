from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from incypher_agent.config import AgentConfig
from incypher_agent.loop import AgentLoop
from incypher_agent.model import ModelReply


class ScriptedModel:
    def __init__(self, script: list[ModelReply]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls += 1
        if not self.script:
            raise AssertionError("scripted model exhausted")
        return self.script.pop(0)


def make_config(root: Path, **overrides: Any) -> AgentConfig:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    values = dict(
        challenge_id="test",
        run_id="run",
        run_dir=root,
        workspace_dir=workspace,
        api_key="test",
        max_seconds=60,
        max_model_calls=10,
        max_tool_calls=10,
        bash_timeout=10,
        max_tool_output=5000,
    )
    values.update(overrides)
    return AgentConfig(**values)


def tool_reply(call_id: str, name: str, arguments: str) -> ModelReply:
    return ModelReply(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        }
    )


class AgentLoopTests(unittest.TestCase):
    def test_success_via_report_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ScriptedModel(
                [
                    tool_reply("call_1", "bash", json.dumps({"command": "echo hello"})),
                    tool_reply("call_2", "report_flag", json.dumps({"flag": "flag{abc}", "evidence": "test"})),
                ]
            )
            loop = AgentLoop(make_config(root), model=model)
            result = loop.run()
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.flag, "flag{abc}")
            self.assertTrue((root / "agent" / "flag.json").exists())

    def test_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ScriptedModel(
                [ModelReply(message={"role": "assistant", "content": "just thinking"})]
            )
            loop = AgentLoop(make_config(root, max_model_calls=1), model=model)
            result = loop.run()
            self.assertEqual(result.status, "HARD_LIMIT")
            self.assertEqual(result.exit_code, 10)
            escalation = json.loads((root / "agent" / "escalation.json").read_text())
            self.assertEqual(escalation["reason"], "hard_limit")


if __name__ == "__main__":
    unittest.main()
