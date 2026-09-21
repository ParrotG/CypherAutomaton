from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arena import main as arena_main


class FakeClient:
    def __init__(self, challenges: list[dict] | None = None) -> None:
        self._challenges = challenges or [
            {"id": 90, "name": "Dear Diary", "category": "(Practice) forensics", "points": 100, "type": "standard"},
            {"id": 95, "name": "Other", "category": "(Practice) web", "points": 100, "type": "dynamic_iac"},
        ]

    def me(self) -> dict:
        return {"solved": []}

    def list_challenges(self) -> list[dict]:
        return self._challenges

    def challenge(self, cid: int) -> dict:
        for ch in self._challenges:
            if int(ch["id"]) == int(cid):
                return dict(ch)
        return {}


class ArenaMainTests(unittest.TestCase):
    def test_main_runs_selected_single_practice_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                calls.append((ch["id"], max_steps, max_attempts, work_root, events_path, prepared_filenames))
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
            self.assertEqual(calls[0][2], 3)
            self.assertEqual(Path(calls[0][4]), Path(temp) / "90" / "events.jsonl")
            self.assertEqual(calls[0][5], [])

    def test_main_auto_mode_selects_practice_when_only_practice_exists(self) -> None:
        challenges = [
            {"id": 90, "name": "P", "category": "(Practice) web", "points": 100, "type": "standard"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                calls.append(ch["id"])
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.1}

            env = {
                "ARENA_MODE": "auto",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", return_value=[]), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [90])

    def test_static_challenges_run_concurrently(self) -> None:
        challenges = [
            {"id": 1, "name": "A", "category": "web", "points": 100, "type": "standard"},
            {"id": 2, "name": "B", "category": "web", "points": 100, "type": "standard"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            lock = threading.Lock()
            state = {"active": 0, "max": 0}

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.1)
                with lock:
                    state["active"] -= 1
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.1}

            env = {
                "ARENA_MODE": "competition",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_CONCURRENT_CHALLENGES": "2",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", return_value=[]), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
        self.assertEqual(code, 0)
        self.assertEqual(state["max"], 2)

    def test_dynamic_challenges_are_serialized(self) -> None:
        challenges = [
            {"id": 3, "name": "C", "category": "web", "points": 100, "type": "dynamic_iac"},
            {"id": 4, "name": "D", "category": "web", "points": 100, "type": "dynamic_iac"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            lock = threading.Lock()
            state = {"active": 0, "max": 0}

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.1)
                with lock:
                    state["active"] -= 1
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.1}

            env = {
                "ARENA_MODE": "competition",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_CONCURRENT_CHALLENGES": "2",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", return_value=[]), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
        self.assertEqual(code, 0)
        self.assertEqual(state["max"], 1)

    def test_mixed_queue_keeps_one_dynamic_and_fills_static(self) -> None:
        challenges = [
            {"id": 1, "name": "D1", "category": "web", "points": 100, "type": "dynamic_iac"},
            {"id": 2, "name": "S1", "category": "web", "points": 100, "type": "standard"},
            {"id": 3, "name": "S2", "category": "web", "points": 100, "type": "standard"},
            {"id": 4, "name": "S3", "category": "web", "points": 100, "type": "standard"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            lock = threading.Lock()
            state = {"dyn_active": 0, "max_dyn": 0, "overlap": False}

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                time.sleep(0.02)
                if ch["type"] == "dynamic_iac":
                    with lock:
                        state["dyn_active"] += 1
                        state["max_dyn"] = max(state["max_dyn"], state["dyn_active"])
                    time.sleep(0.15)
                    with lock:
                        state["dyn_active"] -= 1
                else:
                    time.sleep(0.1)
                    with lock:
                        if state["dyn_active"] > 0:
                            state["overlap"] = True
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.1}

            env = {
                "ARENA_MODE": "competition",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_CONCURRENT_CHALLENGES": "2",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", return_value=[]), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
        self.assertEqual(code, 0)
        self.assertEqual(state["max_dyn"], 1)
        self.assertTrue(state["overlap"])

    def test_timed_out_challenge_is_requeued_to_tail(self) -> None:
        challenges = [
            {"id": 1, "name": "A", "category": "web", "points": 100, "type": "standard"},
            {"id": 2, "name": "B", "category": "web", "points": 100, "type": "standard"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            calls: list[int] = []

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                calls.append(int(ch["id"]))
                if int(ch["id"]) == 1 and calls.count(1) == 1:
                    return {
                        "id": ch["id"],
                        "name": ch["name"],
                        "solved": False,
                        "error": "challenge_timeout",
                        "requeue": True,
                        "seconds": 0.01,
                    }
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.01}

            env = {
                "ARENA_MODE": "competition",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_CONCURRENT_CHALLENGES": "1",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", return_value=[]), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
            results = json.loads((Path(temp) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [1, 2, 1])
        self.assertEqual(results["solved"], 2)

    def test_main_loads_full_challenge_details(self) -> None:
        challenges = [
            {
                "id": 1,
                "name": "Full",
                "category": "web",
                "points": 100,
                "type": "standard",
                "description": "FULL-DESCRIPTION",
                "files": ["https://example.invalid/file.bin"],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            seen = {}

            def fake_prepare(client, ch, cdir):
                seen["prepare_ch"] = ch
                return ["file.bin"]

            def fake_solve(client, ch, *, max_steps, max_attempts, work_root, events_path, prepared_filenames):
                seen["solve_ch"] = ch
                seen["prepared"] = prepared_filenames
                return {"id": ch["id"], "name": ch["name"], "solved": True, "steps": 1, "seconds": 0.1}

            env = {
                "ARENA_MODE": "competition",
                "WORK_ROOT": temp,
                "RESULTS_PATH": str(Path(temp) / "results.json"),
                "MAX_CONCURRENT_CHALLENGES": "1",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "arena.main.connect_from_env", return_value=FakeClient(challenges)
            ), patch("arena.main.prepare_files", side_effect=fake_prepare), patch(
                "arena.main.solve_challenge", side_effect=fake_solve
            ):
                code = arena_main.main()
        self.assertEqual(code, 0)
        self.assertEqual(seen["prepare_ch"]["description"], "FULL-DESCRIPTION")
        self.assertEqual(seen["solve_ch"]["files"], ["https://example.invalid/file.bin"])
        self.assertEqual(seen["prepared"], ["file.bin"])


if __name__ == "__main__":
    unittest.main()
