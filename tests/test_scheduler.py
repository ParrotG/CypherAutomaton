from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from incypher_scheduler.config import SchedulerConfig, parse_worker_command
from incypher_scheduler.scheduler import SimpleScheduler
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
        )
        values.update(overrides)
        return SchedulerConfig(**values)

    async def test_worker_limit_spawns_replacements_until_total_cap(self) -> None:
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "10"}):
            scheduler = SimpleScheduler(self.make_config())
            code = await scheduler.run()
        self.assertEqual(code, 10)
        workers = sorted(path.name for path in (self.root / "scheduler" / "tasks" / "task-test" / "workers").glob("w*"))
        self.assertEqual(workers, ["w0001", "w0002"])

    async def test_success_stops_scheduler(self) -> None:
        with patch.dict(os.environ, {"FAKE_SCHEDULER_EXIT_CODE": "0"}):
            scheduler = SimpleScheduler(
                self.make_config(max_concurrent_workers=2, max_total_workers=5)
            )
            code = await scheduler.run()
        self.assertEqual(code, 0)
        workers_dir = self.root / "scheduler" / "tasks" / "task-test" / "workers"
        workers = sorted(path.name for path in workers_dir.glob("w*"))
        self.assertEqual(workers, ["w0001", "w0002"])


if __name__ == "__main__":
    unittest.main()
