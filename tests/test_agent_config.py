from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from incypher_agent.config import (
    DEFAULT_MODEL,
    AgentConfigError,
    build_agent_config,
)


class AgentConfigTests(unittest.TestCase):
    def make_run_dir(self, root: Path) -> Path:
        run_dir = root / "run"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "challenge_id": "test-challenge",
                    "run_id": "test-run",
                    "agent_endpoint": "tcp://127.0.0.1:12345",
                }
            ),
            encoding="utf-8",
        )
        return run_dir

    def write_env_file(self, root: Path, text: str) -> Path:
        path = root / ".env.local"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_api_key_loaded_only_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run_dir(root)
            env_file = self.write_env_file(root, "DEEPSEEK_API_KEY=from-env-file-1234567890\n")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-process-env-1234567890"}):
                config = build_agent_config(
                    run_dir=run_dir,
                    env_file=env_file,
                    require_api_key=True,
                )
            self.assertEqual(config.api_key, "from-env-file-1234567890")

    def test_missing_env_file_errors_even_if_process_env_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run_dir(root)
            missing = root / "missing.env"
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-process-env-1234567890"}):
                with self.assertRaises(AgentConfigError):
                    build_agent_config(
                        run_dir=run_dir,
                        env_file=missing,
                        require_api_key=True,
                    )

    def test_model_name_is_not_read_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run_dir(root)
            env_file = self.write_env_file(
                root,
                "DEEPSEEK_API_KEY=from-env-file-1234567890\nCYPHER_MODEL=env-model\n",
            )
            config = build_agent_config(
                run_dir=run_dir,
                env_file=env_file,
                require_api_key=True,
            )
            self.assertEqual(config.model, DEFAULT_MODEL)

    def test_invalid_api_key_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run_dir(root)
            env_file = self.write_env_file(root, "DEEPSEEK_API_KEY=bad key\n")
            with self.assertRaises(AgentConfigError):
                build_agent_config(
                    run_dir=run_dir,
                    env_file=env_file,
                    require_api_key=True,
                )


if __name__ == "__main__":
    unittest.main()
