from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena import main as arena_main


class FakeClient:
    def me(self) -> dict:
        return {"solved": [90]}

    def list_challenges(self) -> list[dict]:
        return [
            {"id": 90, "name": "Dear Diary", "category": "(Practice) forensics", "points": 100, "type": "standard"},
            {"id": 95, "name": "Other", "category": "(Practice) web", "points": 100, "type": "dynamic_iac"},
        ]


class ArenaMainTests(unittest.TestCase):
    def test_main_runs_selected_single_practice_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            def fake_solve(client, ch, *, max_steps, work_root, events_path):
                calls.append((ch["id"], max_steps, work_root, events_path))
                return {
                    "id": ch["id"],
                    "name": ch["name"],
                    "solved": True,
                    "steps": 1,
                    "seconds": 0.1,
                }

            env = {
                "ARENA_MODE": "practice",
                "INCLUDE_SOLVED": "1",
                "ONLY_IDS": "90",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_STEPS": "5",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient()
            ), patch("arena.main.solve_challenge", side_effect=fake_solve):
                code = arena_main.main()
            results = json.loads((Path(temp) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(results["attempted"], 1)
            self.assertTrue(results["results"][0]["solved"])
            self.assertEqual(calls[0][0], 90)
            self.assertEqual(calls[0][1], 5)
            self.assertEqual(Path(calls[0][3]), Path(temp) / "90" / "events.jsonl")


if __name__ == "__main__":
    unittest.main()
