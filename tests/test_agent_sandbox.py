from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from incypher_agent.sandbox import BwrapSandbox


class SandboxTests(unittest.TestCase):
    def test_bwrap_hides_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "hello.txt").write_text("inside\n", encoding="utf-8")
            outside = root / "secret.txt"
            outside.write_text("outside-secret\n", encoding="utf-8")
            sandbox = BwrapSandbox(workspace, tool_root=None, network=True)
            command = (
                "cat /workspace/hello.txt; "
                f"cat {outside} 2>/dev/null || echo outside-hidden; "
                "echo private > /tmp/x && cat /tmp/x; "
                "python3 -c 'import socket,struct; print(\"python-ok\")'"
            )
            prepared = sandbox.prepare(command)
            result = subprocess.run(
                prepared.argv,
                cwd=prepared.cwd,
                env=prepared.env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inside", result.stdout)
            self.assertIn("outside-hidden", result.stdout)
            self.assertNotIn("outside-secret", result.stdout)
            self.assertIn("python-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
