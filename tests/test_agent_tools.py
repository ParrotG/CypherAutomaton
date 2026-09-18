from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from incypher_agent.config import AgentConfig
from incypher_agent.tools import ToolExecutor


class AgentToolTests(unittest.TestCase):
    def make_config(self, root: Path) -> AgentConfig:
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return AgentConfig(
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

    def test_bash_and_editor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools = ToolExecutor(self.make_config(root))
            result = tools.execute("bash", {"command": "printf hello"})
            payload = json.loads(result.content)
            self.assertEqual(payload["exit_code"], 0)
            self.assertIn("hello", payload["stdout"])

            created = tools.execute(
                "str_replace_editor",
                {"command": "create", "path": "notes.md", "file_text": "alpha\nbeta\n"},
            )
            self.assertIn("created", created.content)
            replaced = tools.execute(
                "str_replace_editor",
                {"command": "str_replace", "path": "notes.md", "old_str": "beta", "new_str": "gamma"},
            )
            self.assertIn("replaced", replaced.content)
            viewed = tools.execute("str_replace_editor", {"command": "view", "path": "notes.md"})
            self.assertIn("gamma", viewed.content)

    def test_editor_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tools = ToolExecutor(self.make_config(Path(temp)))
            result = tools.execute(
                "str_replace_editor",
                {"command": "view", "path": "../outside.txt"},
            )
            self.assertIn("escapes workspace", result.content)

    def test_report_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tools = ToolExecutor(self.make_config(Path(temp)))
            result = tools.execute("report_flag", {"flag": "flag{test}", "evidence": "unit"})
            self.assertTrue(result.done)
            self.assertEqual(result.flag, "flag{test}")


if __name__ == "__main__":
    unittest.main()
