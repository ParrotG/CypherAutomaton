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

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> ModelReply:
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
        bash_timeout=10,
        max_tool_output=5000,
        sandbox_backend="local",
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


class VerificationModel:
    """Report a candidate, reject once, then report again after feedback."""

    def __init__(self, agent_dir: Path) -> None:
        self.agent_dir = agent_dir
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> ModelReply:
        self.calls += 1
        if self.calls == 1:
            (self.agent_dir / "verification.json").write_text(
                json.dumps({"success": False, "feedback": "try the other CRC"}),
                encoding="utf-8",
            )
            return tool_reply(
                "call_1",
                "report_flag",
                json.dumps({"flag": "flag{f989c670}", "evidence": "first candidate"}),
            )
        if self.calls == 2:
            (self.agent_dir / "verification.json").write_text(
                json.dumps({"success": True, "feedback": ""}),
                encoding="utf-8",
            )
            return tool_reply(
                "call_2",
                "report_flag",
                json.dumps({"flag": "INCYPHER{ad74b76d}", "evidence": "second candidate"}),
            )
        raise AssertionError("verification model exhausted")


class AgentLoopTests(unittest.TestCase):
    def test_success_via_report_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_dir = root / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "verification.json").write_text(
                json.dumps({"success": True, "feedback": ""}),
                encoding="utf-8",
            )
            model = ScriptedModel(
                [
                    tool_reply("call_1", "bash", json.dumps({"command": "echo hello"})),
                    tool_reply("call_2", "report_flag", json.dumps({"flag": "flag{abc}", "evidence": "test"})),
                ]
            )
            loop = AgentLoop(make_config(root), model=model)
            result = loop.run()
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.flag, "INCYPHER{abc}")
            self.assertTrue((root / "agent" / "flag.json").exists())

    def test_no_wait_verification_exits_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ScriptedModel(
                [
                    tool_reply(
                        "call_1",
                        "report_flag",
                        json.dumps({"flag": "flag{direct}", "evidence": "direct"}),
                    )
                ]
            )
            loop = AgentLoop(
                make_config(root, wait_for_verification=False),
                model=model,
            )
            result = loop.run()
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.flag, "INCYPHER{direct}")
            self.assertFalse((root / "agent" / "verification.json").exists())

    def test_rejected_candidate_feedback_resumes_same_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_dir = root / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            model = VerificationModel(agent_dir)
            loop = AgentLoop(make_config(root), model=model)
            result = loop.run()
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.flag, "INCYPHER{ad74b76d}")
            events = [
                json.loads(line)
                for line in (agent_dir / "events.jsonl").read_text().splitlines()
                if line.strip()
            ]
            kinds = [event["kind"] for event in events]
            self.assertIn("verification_failed", kinds)

    def test_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ScriptedModel(
                [
                    ModelReply(
                        message={"role": "assistant", "content": "just thinking"},
                        prompt_tokens=5000,
                        completion_tokens=100,
                        total_tokens=5100,
                    )
                ]
            )
            loop = AgentLoop(
                make_config(
                    root,
                    context_window_tokens=10_000,
                    context_reserve_tokens=9_000,
                ),
                model=model,
            )
            result = loop.run()
            self.assertEqual(result.status, "CONTEXT_LIMIT")
            self.assertEqual(result.exit_code, 10)
            escalation = json.loads((root / "agent" / "escalation.json").read_text())
            self.assertEqual(escalation["reason"], "context_limit")
            self.assertGreaterEqual(
                escalation["estimated_prompt_tokens"],
                escalation["context_limit_tokens"],
            )


if __name__ == "__main__":
    unittest.main()
