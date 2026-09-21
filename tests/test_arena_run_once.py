from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.run_once import main


class FakeClient:
    def challenge(self, cid: int) -> dict:
        return {"id": cid, "name": "Zip", "type": "standard"}


class ArenaRunOnceTests(unittest.TestCase):
    def test_writes_result_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch("arena.run_once.connect_from_env", return_value=FakeClient()), patch(
                "arena.run_once.solve_challenge",
                return_value={
                    "id": 94,
                    "name": "Zip",
                    "solved": True,
                    "steps": 3,
                    "seconds": 1.2,
                },
            ):
                code = main(["--challenge-id", "94", "--work-root", temp])
            result_file = Path(temp) / "result-94.json"
            self.assertEqual(code, 0)
            self.assertTrue(result_file.is_file())
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertTrue(payload["solved"])

    def test_returns_failure_when_not_solved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch("arena.run_once.connect_from_env", return_value=FakeClient()), patch(
                "arena.run_once.solve_challenge",
                return_value={"id": 94, "name": "Zip", "solved": False},
            ):
                code = main(["--challenge-id", "94", "--work-root", temp])
        self.assertEqual(code, 10)


if __name__ == "__main__":
    unittest.main()
