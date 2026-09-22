from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "arena"))

from deploy.badle_hard import entrypoint as submission  # noqa: E402
from d2_badle import BADLE_HARD_FLAG_SHA256  # noqa: E402


ARTIFACTS = ROOT / "arena-runs" / "d2-intake-20260922-1" / "4"
FILENAMES = ("APKlog.zip", "cgm_ble_log.txt")
BASE = "https://platform.example.test"


def _challenge(**changes):
    challenge = {
        "id": 4,
        "name": "BadLE Hard",
        "category": "healthcare",
        "type": "standard",
        "description": "Offline supplied BLE capture.",
        "files": [
            "/files/archive/APKlog.zip",
            "/files/capture/cgm_ble_log.txt",
        ],
    }
    challenge.update(changes)
    return challenge


class BadLEHardSubmissionContractTests(unittest.TestCase):
    def test_dockerfile_routes_entrypoint_and_direct_main_to_the_static_profile(self):
        dockerfile = (ROOT / "deploy" / "badle_hard" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        source = "COPY deploy/badle_hard/entrypoint.py"
        self.assertIn(source + " /opt/agent/badle_hard_entry.py", dockerfile)
        self.assertIn(source + " /opt/agent/main.py", dockerfile)
        self.assertIn(
            'ENTRYPOINT ["python3", "-B", "/opt/agent/badle_hard_entry.py"]',
            dockerfile,
        )

    def test_accepts_only_the_static_controller_identity(self):
        submission.validate_identity(_challenge())
        changes = (
            {"id": 3},
            {"id": True},
            {"name": "BadLE"},
            {"category": "crypto"},
            {"type": "dynamic_iac"},
            {"connection": "https://instance.example.test"},
            {"connectionInfo": "host.example.test:1234"},
            {"connection_info": "https://instance.example.test"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(submission.ProfileError):
                submission.validate_identity(_challenge(**change))

    def test_missing_identity_is_rejected(self):
        for field in ("id", "name", "category", "type"):
            challenge = _challenge()
            del challenge[field]
            with self.subTest(field=field), self.assertRaises(submission.ProfileError):
                submission.validate_identity(challenge)

    def test_artifact_urls_bind_both_names_to_the_same_https_origin(self):
        challenge = _challenge(files=[
            "/files/archive/APKlog.zip?token=example-only",
            BASE + "/files/capture/cgm_ble_log.txt",
        ])
        urls = submission.validate_artifact_urls(challenge, BASE)
        self.assertEqual(urls, {
            "APKlog.zip": BASE + "/files/archive/APKlog.zip?token=example-only",
            "cgm_ble_log.txt": BASE + "/files/capture/cgm_ble_log.txt",
        })

    def test_unsafe_or_nonexact_artifact_url_sets_are_rejected(self):
        valid = _challenge()["files"]
        cases = (
            [],
            valid[:1],
            valid + ["/files/extra.txt"],
            [valid[0], valid[0]],
            ["https://other.example.test/APKlog.zip", valid[1]],
            ["http://platform.example.test/APKlog.zip", valid[1]],
            ["https://user:password@platform.example.test/APKlog.zip", valid[1]],
            [BASE + ":444/APKlog.zip", valid[1]],
            ["/files/not-APKlog.zip", valid[1]],
            ["/files/APKlog.zip#fragment", valid[1]],
            ["/files/folder%2FAPKlog.zip", valid[1]],
        )
        for files in cases:
            with self.subTest(files=files), self.assertRaises(submission.ProfileError):
                submission.validate_artifact_urls(_challenge(files=files), BASE)


@unittest.skipUnless(
    all((ARTIFACTS / name).is_file() for name in FILENAMES),
    "exact D2 BadLE Hard intake artifacts are not present",
)
class BadLEHardSubmissionReplayTests(unittest.TestCase):
    def _solve(self, challenge, artifact_dir, work_root, **kwargs):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            redirect_stderr(output),
            patch("socket.socket.connect", side_effect=AssertionError("offline replay used network")),
            patch("subprocess.Popen", side_effect=AssertionError("offline replay used a subprocess")),
            patch.dict(sys.modules, {"ctfd": None, "brain": None, "solver": None}),
        ):
            result = submission.solve_local(challenge, artifact_dir, work_root, **kwargs)
        self.assertNotIn("flag{", output.getvalue())
        self.assertNotIn("flag{", json.dumps(result))
        self.assertNotIn("INCYPHER{", output.getvalue())
        self.assertNotIn("INCYPHER{", json.dumps(result))
        return result

    def _assert_rejected(self, result):
        self.assertFalse(result["solved"])
        self.assertFalse(result["candidate_found"])
        self.assertFalse(result["submission_attempted"])
        self.assertNotEqual(result["final"], "candidate_recorded")

    def test_download_checks_exact_log_bytes_without_a_second_request(self):
        url = BASE + "/files/cgm_ble_log.txt"
        data = (ARTIFACTS / "cgm_ble_log.txt").read_bytes()
        response = io.BytesIO(data)
        response.geturl = lambda: url
        opener = Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cgm_ble_log.txt"
            with (
                patch.object(submission.urllib.request, "build_opener", return_value=opener),
                patch("socket.socket.connect", side_effect=AssertionError("download test used network")),
            ):
                submission._download(url, output, "cgm_ble_log.txt", "example-only", "test-agent")
            self.assertEqual(output.read_bytes(), data)
            opener.open.assert_called_once()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, url)

    def test_download_rejects_redirect_size_and_digest_mismatches(self):
        url = BASE + "/files/cgm_ble_log.txt"
        data = (ARTIFACTS / "cgm_ble_log.txt").read_bytes()
        mutated = bytes([data[0] ^ 1]) + data[1:]
        cases = (
            ("redirect", data, "https://other.example.test/cgm_ble_log.txt"),
            ("oversize", data + b"x", url),
            ("truncated", data[:-1], url),
            ("digest", mutated, url),
        )
        for label, content, response_url in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                response = io.BytesIO(content)
                response.geturl = lambda: response_url
                opener = Mock()
                opener.open.return_value = response
                with (
                    patch.object(submission.urllib.request, "build_opener", return_value=opener),
                    patch("socket.socket.connect", side_effect=AssertionError("download test used network")),
                    self.assertRaises(submission.ProfileError),
                ):
                    submission._download(
                        url, Path(directory) / "cgm_ble_log.txt", "cgm_ble_log.txt",
                        "example-only", "test-agent",
                    )
                opener.open.assert_called_once()

    def test_default_replay_records_exact_candidate_even_with_submit_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            calls = []
            with patch.dict(os.environ, {"ARENA_SUBMIT_MODE": "submit"}):
                result = self._solve(
                    _challenge(), ARTIFACTS, work,
                    submit_flag=lambda value: calls.append(value) or {"status": "correct"},
                )
            self.assertEqual(calls, [])
            self.assertEqual(result["id"], 4)
            self.assertFalse(result["solved"])
            self.assertTrue(result["candidate_found"])
            self.assertFalse(result["submission_attempted"])
            self.assertEqual(result["candidate_sha256"], BADLE_HARD_FLAG_SHA256)
            self.assertEqual(result["final"], "candidate_recorded")
            self.assertEqual(result["steps"], 1)

            ledgers = list(work.rglob(".arena-candidates.jsonl"))
            self.assertEqual(len(ledgers), 1)
            records = [json.loads(line) for line in ledgers[0].read_text().splitlines()]
            self.assertEqual(len(records), 1)
            candidate = records[0]["candidate"]
            self.assertEqual(
                hashlib.sha256(candidate.encode("ascii")).hexdigest(),
                BADLE_HARD_FLAG_SHA256,
            )
            evidence = list(work.rglob(".arena-evidence.jsonl"))
            self.assertTrue(evidence)
            for ledger in evidence:
                text = ledger.read_text()
                self.assertNotIn(candidate, text)
                events = [json.loads(line) for line in text.splitlines()]
                self.assertTrue(any(event["action"] == "deterministic" for event in events))

    def test_submit_verdicts_invoke_callback_once_and_do_not_echo_response_details(self):
        cases = (
            ("correct", True, "platform_confirmed"),
            ("already_solved", True, "account_already_solved"),
            ("incorrect", False, "candidate_rejected"),
            ("error", False, "submission_unconfirmed"),
        )
        for status, solved, final in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                calls = []

                def submit(value):
                    calls.append(value)
                    return {"status": status, "message": "flag{must-not-enter-results}", "token": "example-sensitive-value"}

                result = self._solve(
                    _challenge(), ARTIFACTS, Path(directory) / "work",
                    submit_mode="submit", submit_flag=submit,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(hashlib.sha256(calls[0].encode("ascii")).hexdigest(), BADLE_HARD_FLAG_SHA256)
                self.assertEqual(result["solved"], solved)
                self.assertEqual(result["final"], final)
                self.assertEqual(result["verdict"], {"status": status})
                self.assertTrue(result["candidate_found"])
                self.assertTrue(result["submission_attempted"])
                self.assertEqual(result["candidate_sha256"], BADLE_HARD_FLAG_SHA256)
                self.assertNotIn("example-sensitive-value", json.dumps(result))
                for ledger in (Path(directory) / "work").rglob(".arena-evidence.jsonl"):
                    self.assertNotIn(calls[0], ledger.read_text())
                    self.assertNotIn("example-sensitive-value", ledger.read_text())

    def test_callback_exception_is_terminal_without_second_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def submit(value):
                calls.append(value)
                raise RuntimeError("flag{exception-must-not-enter-results}")

            result = self._solve(
                _challenge(), ARTIFACTS, Path(directory) / "work",
                submit_mode="submit", submit_flag=submit,
            )
            self.assertEqual(len(calls), 1)
            self.assertFalse(result["solved"])
            self.assertTrue(result["submission_attempted"])
            self.assertTrue(result["candidate_found"])
            self.assertEqual(result["final"], "submission_unconfirmed")
            self.assertEqual(result["verdict"], {"status": "error"})

    def test_post_submission_processing_failure_preserves_uncertain_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def fail_after_submission(executor, *, flag, **_kwargs):
                executor._submit_flag(flag)
                raise RuntimeError("private processing failure")

            with patch.object(
                submission.ActionExecutor,
                "record_deterministic_candidate",
                fail_after_submission,
            ):
                result = self._solve(
                    _challenge(), ARTIFACTS, Path(directory) / "work",
                    submit_mode="submit",
                    submit_flag=lambda value: calls.append(value) or {"status": "error"},
                )
            self.assertEqual(len(calls), 1)
            self.assertTrue(result["submission_attempted"])
            self.assertFalse(result["solved"])
            self.assertEqual(result["verdict"], {"status": "error"})
            self.assertEqual(result["final"], "submission_unconfirmed")
            self.assertEqual(result["error"], "local_processing_failed")

    def test_unsupported_mode_or_missing_submit_callback_rejects(self):
        for kwargs in ({"submit_mode": "automatic"}, {"submit_mode": "submit"}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                result = self._solve(_challenge(), ARTIFACTS, Path(directory) / "work", **kwargs)
                self._assert_rejected(result)

    def test_identity_mismatch_cannot_reach_artifacts_or_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._solve(
                _challenge(type="dynamic_iac"), Path(directory) / "missing",
                Path(directory) / "work", submit_mode="submit",
                submit_flag=lambda value: self.fail("mismatched identity submitted"),
            )
            self._assert_rejected(result)
            self.assertFalse((Path(directory) / "work" / "4").exists())

    def test_extra_local_files_reject_before_candidate_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for name in FILENAMES:
                shutil.copyfile(ARTIFACTS / name, source / name)
            (source / "unexpected.txt").write_text("unrelated data", encoding="utf-8")
            result = self._solve(
                _challenge(), source, Path(directory) / "work", submit_mode="submit",
                submit_flag=lambda value: self.fail("extra artifact submitted"),
            )
            self._assert_rejected(result)

    def test_digest_mismatch_rejects_without_model_or_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for name in FILENAMES:
                shutil.copyfile(ARTIFACTS / name, source / name)
            log = source / "cgm_ble_log.txt"
            data = bytearray(log.read_bytes())
            data[len(data) // 2] ^= 1
            log.write_bytes(data)
            result = self._solve(
                _challenge(), source, Path(directory) / "work", submit_mode="submit",
                submit_flag=lambda value: self.fail("mutated artifact submitted"),
            )
            self._assert_rejected(result)
            self.assertFalse(list((Path(directory) / "work").rglob(".arena-candidates.jsonl")))

    def test_existing_challenge_directory_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            existing = work / "4"
            existing.mkdir(parents=True)
            marker = existing / "keep.txt"
            marker.write_text("previous run", encoding="utf-8")
            result = self._solve(
                _challenge(), ARTIFACTS, work, submit_mode="submit",
                submit_flag=lambda value: self.fail("existing workdir submitted"),
            )
            self._assert_rejected(result)
            self.assertEqual(marker.read_text(), "previous run")
            self.assertEqual(set(path.name for path in existing.iterdir()), {"keep.txt"})

    def test_replay_cli_forces_record_without_platform_or_model_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            output = io.StringIO()
            with (
                patch.dict(os.environ, {
                    "ARENA_SUBMIT_MODE": "submit",
                    "ONLY_IDS": "19,42,106",
                    "CTF_TOKEN": "example-token-must-not-be-used",
                    "CTF_BASE": BASE,
                    "LLM_API_KEY": "example-key-must-not-be-used",
                }),
                patch.dict(sys.modules, {"ctfd": None, "brain": None, "solver": None}),
                patch("socket.socket.connect", side_effect=AssertionError("replay CLI used network")),
                patch("subprocess.Popen", side_effect=AssertionError("replay CLI used subprocess")),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                exit_code = submission.main([
                    "--replay-dir", str(ARTIFACTS), "--work-root", str(work),
                ])
            self.assertEqual(exit_code, 0)
            result = json.loads((work / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(result["mode"], "offline_replay")
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["solved"], 0)
            challenge = result["results"][0]
            self.assertEqual(challenge["id"], 4)
            self.assertEqual(challenge["final"], "candidate_recorded")
            self.assertEqual(challenge["candidate_sha256"], BADLE_HARD_FLAG_SHA256)
            self.assertFalse(challenge["submission_attempted"])
            self.assertFalse(challenge["had_instance"])
            self.assertEqual(challenge["submit_mode"], "record")
            self.assertNotIn("flag{", output.getvalue())
            self.assertNotIn("INCYPHER{", output.getvalue())
            self.assertNotIn("example-token-must-not-be-used", output.getvalue())
            self.assertNotIn("example-key-must-not-be-used", output.getvalue())


if __name__ == "__main__":
    unittest.main()
