from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from incypher_scheduler.cli import main as scheduler_cli_main
from incypher_scheduler.config import SchedulerConfig, parse_worker_command
from incypher_scheduler.scheduler import SimpleScheduler
from incypher_scheduler.targets import TargetKind, classify_target
from tests.mock_gate import MockGate


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gate = MockGate(team_key=b"test-team-key", bits=8).start()
        self.env_file = self.root / ".env.local"
        self.env_file.write_text("CYPHER_TEAM_KEY=test-team-key\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        self.fake_worker = Path(__file__).with_name("fake_scheduler_worker.py").resolve()
        self.worker_command = parse_worker_command(
            json.dumps([sys.executable, str(self.fake_worker)])
        )

    def tearDown(self) -> None:
        self.gate.stop()
        self.temp.cleanup()

    def make_config(self, **overrides) -> SchedulerConfig:
        values = dict(
            task_id="task-test",
            target=f"{self.gate.host}:{self.gate.port}",
            state_dir=self.root / "scheduler",
            max_concurrent_workers=1,
            max_total_workers=2,
            description_text="scheduler test task",
            env_file=str(self.env_file),
            worker_command=self.worker_command,
            sandbox_backend="local",
        )
        values.update(overrides)
        return SchedulerConfig(**values)

    def current_attempt(self) -> Path:
        roots = list(
            (self.root / "scheduler" / "tasks" / "task-test" / "attempts").glob(
                "attempt-*"
            )
        )
        self.assertEqual(len(roots), 1, roots)
        return roots[0]

    async def test_worker_limit_spawns_replacements_until_total_cap(self) -> None:
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "10"}):
            scheduler = SimpleScheduler(self.make_config())
            code = await scheduler.run()
        self.assertEqual(code, 10)
        workers = sorted(path.name for path in (self.current_attempt() / "workers").glob("w*"))
        self.assertEqual(workers, ["w0001", "w0002"])
        records = list(
            (
                self.root
                / "scheduler"
                / "tasks"
                / "task-test"
                / "blackboard"
                / "records"
            ).glob("system-*.json")
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(json.loads(record.read_text())["exit_code"] for record in records),
            [10, 10],
        )

    def test_manual_review_writes_verification(self) -> None:
        agent_dir = (
            self.root
            / "scheduler"
            / "tasks"
            / "task-test"
            / "attempts"
            / "attempt-manual"
            / "workers"
            / "w0001"
            / "agent"
        )
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "candidate.json").write_text(
            json.dumps({"flag": "INCYPHER{abc}"}),
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = scheduler_cli_main(
                [
                    "review",
                    "--task-id",
                    "task-test",
                    "--run-id",
                    "w0001",
                    "--result",
                    "failed",
                    "--feedback",
                    "try again",
                    "--state-dir",
                    str(self.root / "scheduler"),
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        verification = json.loads((agent_dir / "verification.json").read_text())
        self.assertFalse(verification["success"])
        self.assertEqual(verification["feedback"], "try again")

    def test_classify_target_types(self) -> None:
        self.assertEqual(classify_target("127.0.0.1:1234").kind, TargetKind.RAW_TCP)
        self.assertEqual(classify_target("https://example.com/x").kind, TargetKind.URL)
        self.assertEqual(classify_target(str(self.root)).kind, TargetKind.FILES)

    async def test_url_target_runs_without_bridge(self) -> None:
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "0"}):
            scheduler = SimpleScheduler(
                self.make_config(
                    target="https://example.com/challenge",
                    max_concurrent_workers=1,
                    max_total_workers=2,
                )
            )
            code = await scheduler.run()
        self.assertEqual(code, 0)
        worker_dir = self.current_attempt() / "workers" / "w0001"
        payload = json.loads((worker_dir / "fake_worker.json").read_text())
        self.assertIn("--target", payload["argv"])
        self.assertIn("https://example.com/challenge", payload["argv"])
        self.assertNotIn("--agent-endpoint", payload["argv"])
        records = list(
            (
                self.root
                / "scheduler"
                / "tasks"
                / "task-test"
                / "blackboard"
                / "records"
            ).glob("system-*.json")
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["exit_code"], 0)

    async def test_file_target_copies_files_and_runs_without_bridge(self) -> None:
        source = self.root / "challenge-files"
        source.mkdir()
        (source / "message.txt").write_text("file-target", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "0"}):
            scheduler = SimpleScheduler(
                self.make_config(
                    target=str(source),
                    max_concurrent_workers=1,
                    max_total_workers=1,
                )
            )
            code = await scheduler.run()
        self.assertEqual(code, 0)
        worker_dir = self.current_attempt() / "workers" / "w0001"
        copied = worker_dir / "agent" / "workspace" / "challenge_files" / "message.txt"
        self.assertEqual(copied.read_text(encoding="utf-8"), "file-target")
        payload = json.loads((worker_dir / "fake_worker.json").read_text())
        self.assertIn("--target", payload["argv"])
        self.assertIn("/workspace/challenge_files", payload["argv"])
        self.assertNotIn("--agent-endpoint", payload["argv"])

    async def test_success_stops_scheduler(self) -> None:
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "0"}):
            scheduler = SimpleScheduler(
                self.make_config(max_concurrent_workers=2, max_total_workers=5)
            )
            code = await scheduler.run()
        self.assertEqual(code, 0)
        workers_dir = self.current_attempt() / "workers"
        workers = sorted(path.name for path in workers_dir.glob("w*"))
        self.assertEqual(workers, ["w0001", "w0002"])


if __name__ == "__main__":
    unittest.main()
