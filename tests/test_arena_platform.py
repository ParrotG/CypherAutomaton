from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.official.ctfd import CTFdClient
from arena.platform import RetryingCTFdClient, is_practice, resolve_mode, select_targets
from arena.solver import (
    _renew_loop,
    build_prompt,
    default_work_root,
    make_run_bash,
    prepare_files,
    solve_challenge,
)


class FakeClient:
    def __init__(self) -> None:
        self.booted: list[int] = []
        self.destroyed: list[int] = []
        self.submitted: list[tuple[int, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.renewed: list[int] = []

    def download(self, url: str, dest: str) -> None:
        self.downloads.append((url, dest))
        Path(dest).write_bytes(b"challenge-data")

    def boot(self, cid: int) -> dict:
        self.booted.append(cid)
        return {"connectionInfo": f"http://127.0.0.1/{cid}"}

    def instance_status(self, cid: int) -> dict:
        return {"connectionInfo": f"http://127.0.0.1/{cid}"}

    def destroy(self, cid: int) -> bool:
        self.destroyed.append(cid)
        return True

    def renew(self, cid: int) -> dict:
        self.renewed.append(cid)
        return {"renewed": True}

    def submit(self, cid: int, flag: str) -> dict:
        self.submitted.append((cid, flag))
        return {"status": "correct"}


class FakeBrain:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompt = ""

    def solve(self, prompt: str) -> dict:
        self.prompt = prompt
        return {"solved": True, "steps": 1, "flag": "INCYPHER{test}"}


class SlowFakeBrain(FakeBrain):
    def solve(self, prompt: str) -> dict:
        time.sleep(0.08)
        return super().solve(prompt)


class RetryBrain:
    instances: list["RetryBrain"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompt = ""
        RetryBrain.instances.append(self)

    def solve(self, prompt: str) -> dict:
        self.prompt = prompt
        if len(RetryBrain.instances) == 1:
            return {"solved": False, "steps": 1, "error": "context_limit"}
        return {"solved": True, "steps": 1, "flag": "INCYPHER{retry}"}


class SubmitBrain:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def solve(self, prompt: str) -> dict:
        verdict = self.kwargs["submit_flag"]("INCYPHER{test}")
        return {
            "solved": verdict.get("status") == "correct",
            "steps": 1,
            "verdict": verdict,
        }


class RecordingBrain:
    records: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        RecordingBrain.records.append(kwargs)

    def solve(self, prompt: str) -> dict:
        return {"solved": False, "steps": 1, "error": "not_solved"}


class ArenaPlatformTests(unittest.TestCase):
    def test_practice_detection(self) -> None:
        self.assertTrue(is_practice({"category": "(Practice) web"}))
        self.assertFalse(is_practice({"category": "web"}))

    def test_auto_mode_prefers_competition_when_present(self) -> None:
        rows = [
            {"id": 1, "category": "(Practice) web"},
            {"id": 2, "category": "web"},
        ]
        self.assertEqual(resolve_mode(rows, "auto"), "competition")

    def test_auto_mode_falls_back_to_practice(self) -> None:
        rows = [{"id": 1, "category": "(Practice) web"}]
        self.assertEqual(resolve_mode(rows, "auto"), "practice")

    def test_default_work_root_respects_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"WORK_ROOT": temp}, clear=False
        ):
            self.assertEqual(default_work_root(), Path(temp).resolve())

    def test_renew_loop_ticks_and_stops(self) -> None:
        client = FakeClient()
        stop = threading.Event()
        thread = threading.Thread(
            target=_renew_loop, args=(client, 42, stop, 0.01), daemon=True
        )
        thread.start()
        time.sleep(0.05)
        stop.set()
        thread.join(timeout=1)
        self.assertIn(42, client.renewed)
        self.assertFalse(thread.is_alive())

    def test_solve_challenge_renews_dynamic_instance(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"RENEW_INTERVAL_SECONDS": "0.01"}, clear=False
        ):
            result = solve_challenge(
                client,
                {
                    "id": 42,
                    "name": "Parcelport",
                    "category": "(Practice) web",
                    "points": 500,
                    "type": "dynamic_iac",
                    "description": "web",
                    "files": [],
                },
                max_steps=3,
                brain_factory=SlowFakeBrain,
                work_root=Path(temp),
            )
        self.assertTrue(result["solved"])
        self.assertIn(42, client.renewed)
        self.assertEqual(client.destroyed, [42])

    def test_retrying_client_retries_transient_errors(self) -> None:
        client = RetryingCTFdClient(
            "https://example.invalid",
            "token",
            max_attempts=3,
            retry_base=0.001,
            retry_max_wait=0.002,
        )
        with patch.object(
            CTFdClient,
            "_call",
            side_effect=[(500, {"ok": False}), (200, {"ok": True, "data": {}})],
        ):
            code, payload = client._call("GET", "/x")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_select_targets_filters_and_sorts(self) -> None:
        rows = [
            {"id": 3, "category": "(Practice) web", "points": 100},
            {"id": 1, "category": "pwn", "points": 500},
            {"id": 2, "category": "web", "points": 100},
            {"id": 4, "category": "web", "points": 100},
        ]
        competition = select_targets(rows, mode="competition", solved_ids=[4])
        self.assertEqual([row["id"] for row in competition], [2, 1])
        practice = select_targets(rows, mode="practice", include_solved=True)
        self.assertEqual([row["id"] for row in practice], [3])
        only = select_targets(rows, mode="competition", only_ids=[1, 2], include_solved=True)
        self.assertEqual([row["id"] for row in only], [2, 1])



    def test_disabled_submit_flags_are_intercepted(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"SUBMIT_FLAGS": "0"}, clear=False
        ):
            result = solve_challenge(
                client,
                {
                    "id": 99,
                    "name": "Test",
                    "category": "web",
                    "points": 100,
                    "type": "standard",
                    "description": "test",
                    "files": [],
                },
                max_steps=3,
                max_attempts=1,
                brain_factory=SubmitBrain,
                work_root=Path(temp),
            )
        self.assertTrue(result["solved"])
        self.assertFalse(result["submit_flags"])
        self.assertEqual(client.submitted, [])
        self.assertTrue(result["verdict"]["submission_disabled"])

    def test_solve_challenge_retries_with_summary(self) -> None:
        RetryBrain.instances = []
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"AGENTS_PER_CHALLENGE": "1"}, clear=False
        ):
            result = solve_challenge(
                client,
                {
                    "id": 90,
                    "name": "Dear Diary",
                    "category": "(Practice) forensics",
                    "points": 100,
                    "type": "standard",
                    "description": "diary",
                    "files": [],
                },
                max_steps=3,
                max_attempts=2,
                brain_factory=RetryBrain,
                work_root=Path(temp),
            )
            summary_file = Path(temp) / "90" / "attempts" / "001" / "summary.txt"
            self.assertTrue(summary_file.is_file())
            second_prompt = RetryBrain.instances[1].prompt
        self.assertTrue(result["solved"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertIn("Previous attempt summary", second_prompt)

    def test_multi_agent_uses_independent_workspaces(self) -> None:
        RecordingBrain.records = []
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"AGENTS_PER_CHALLENGE": "2"}, clear=False
        ):
            result = solve_challenge(
                client,
                {
                    "id": 90,
                    "name": "Dear Diary",
                    "category": "(Practice) forensics",
                    "points": 100,
                    "type": "standard",
                    "description": "diary",
                    "files": [],
                },
                max_steps=1,
                max_attempts=1,
                brain_factory=RecordingBrain,
                work_root=Path(temp),
            )
        workspaces = {record["workspace_dir"] for record in RecordingBrain.records}
        self.assertEqual(len(workspaces), 2)
        self.assertTrue(all("/agents/agent-" in path for path in workspaces))
        self.assertEqual(result["agents_per_challenge"], 2)

    def test_agent_workspace_path_policy_blocks_escapes(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            agent_dir = Path(temp) / "90" / "agents" / "agent-0"
            agent_dir.mkdir(parents=True)
            (agent_dir / "ok.txt").write_text("ok", encoding="utf-8")
            run_bash = make_run_bash(agent_dir)
            self.assertIn("blocked", run_bash("cat ../shared/secret"))
            self.assertIn("blocked", run_bash("cat /work/90/shared/secret"))
            self.assertIn("blocked", run_bash("cat /work/90/agents/agent-1/other"))
            self.assertIn("ok", run_bash("cat ok.txt"))

    def test_prepare_files_downloads_into_challenge_dir(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            names = prepare_files(
                client,
                {"files": ["https://example.invalid/a.bin?token=x"]},
                Path(temp) / "94",
            )
            self.assertEqual(names, ["a.bin"])
            self.assertTrue((Path(temp) / "94" / "a.bin").is_file())
        self.assertEqual(len(client.downloads), 1)


    def test_bash_policy_blocks_root_scans_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_bash = make_run_bash(Path(temp))
            self.assertIn("blocked", run_bash("find /"))
            self.assertIn("blocked", run_bash("grep -r rootme /"))
            self.assertIn("blocked", run_bash("cat events.jsonl"))
            self.assertIn("blocked", run_bash("find /work"))
            self.assertIn("ok", run_bash("printf ok"))

    def test_build_prompt_mentions_pow_helper(self) -> None:
        prompt = build_prompt(
            {
                "id": 106,
                "name": "Overflow Ward",
                "category": "(Practice) pwn",
                "points": 250,
                "type": "dynamic_iac",
                "description": "pwn it",
            },
            cdir=Path("/work/106"),
            filenames=[],
            connection="nc 1.2.3.4 1234 (PoW-gated: team_key=redacted)",
        )
        self.assertIn("connect_pwn", prompt)
        self.assertIn("/work/106", prompt)

    def test_solve_challenge_dynamic_boots_and_destroys(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            result = solve_challenge(
                client,
                {
                    "id": 42,
                    "name": "Parcelport",
                    "category": "(Practice) web",
                    "points": 500,
                    "type": "dynamic_iac",
                    "description": "web",
                    "files": [],
                },
                max_steps=3,
                brain_factory=FakeBrain,
                work_root=Path(temp),
            )
        self.assertTrue(result["solved"])
        self.assertEqual(client.booted, [42])
        self.assertEqual(client.destroyed, [42])
        self.assertEqual(result["id"], 42)

    def test_solve_challenge_static_does_not_boot(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            result = solve_challenge(
                client,
                {
                    "id": 94,
                    "name": "Zip",
                    "category": "(Practice) forensics",
                    "points": 100,
                    "type": "standard",
                    "description": "zip",
                    "files": [],
                },
                max_steps=3,
                brain_factory=FakeBrain,
                work_root=Path(temp),
            )
        self.assertTrue(result["solved"])
        self.assertEqual(client.booted, [])
        self.assertEqual(client.destroyed, [])


if __name__ == "__main__":
    unittest.main()
