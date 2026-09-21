from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "arena" / "official"
EXPECTED = {
    "CONTRACT.md": "a097df26648adff04c925d0f87f57e96adcc4eb59c70b46a9e65c25be17a085f",
    "README.md": "874938b8b1ec6f9358a25c96fc0b6dac4f95701a634e7bd19f0ea7f8eeba1d1d",
    "brain.py": "669a55f30d7287b0633226cc5424ff49866c554024e181780ff047049de2842a",
    "check_agent.sh": "c50ca1f8f0db895a4118fff9b87d48fd9911bebf1cb6537e8cd3bb305783c544",
    "ctfd.py": "a328511e7bbb61f95f741b5097f309a73f090ec74bac47510851e06af310fcbf",
    "local_test.py": "f496c6aed5701a46526215eb3f633fc22ff6c7dc6db9f21a92aa8bbb624fafb6",
    "main.py": "1329c65ab37c953fbb74d37057adf0d325ee4340473069471379777bd8aeddf0",
    "solver.py": "2046a613283065730fc47886bd34a74d74bc3e80b4a0860f1ce9113b3d2d8f22",
}

class OfficialBaselineTests(unittest.TestCase):
    def test_official_files_are_present_and_pinned(self) -> None:
        for name, expected in EXPECTED.items():
            path = BASE / name
            self.assertTrue(path.is_file(), f"missing {path}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, expected, f"hash mismatch for {name}")

    def test_official_entrypoint_contract_is_present(self) -> None:
        text = (BASE / "CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("class Brain", text)
        self.assertIn("run_bash", text)
        self.assertIn("submit_flag", text)
        self.assertIn("/work/results.json", text)


if __name__ == "__main__":
    unittest.main()
