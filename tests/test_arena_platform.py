from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arena.platform import is_practice, select_targets
from arena.solver import build_prompt, prepare_files, solve_challenge


class FakeClient:
    def __init__(self) -> None:
        self.booted: list[int] = []
        self.destroyed: list[int] = []
        self.submitted: list[tuple[int, str]] = []
        self.downloads: list[tuple[str, str]] = []

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


class ArenaPlatformTests(unittest.TestCase):
    def test_practice_detection(self) -> None:
        self.assertTrue(is_practice({"category": "(Practice) web"}))
        self.assertFalse(is_practice({"category": "web"}))

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


    def test_solve_challenge_retries_with_summary(self) -> None:
        RetryBrain.instances = []
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
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
            summary_file = Path(temp) / "90" / "attempts" / "001" / "summary.json"
            self.assertTrue(summary_file.is_file())
            second_prompt = RetryBrain.instances[1].prompt
        self.assertTrue(result["solved"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertIn("Previous attempt summary", second_prompt)

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
