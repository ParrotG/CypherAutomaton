"""Single-challenge static fallback for BadLE Hard; no model or instance actions."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable
import urllib.parse
import urllib.request


# The source checkout keeps these modules in a sibling directory. The image
# copies them next to this entrypoint, where ordinary imports find them.
_arena = Path(__file__).resolve().parents[1] / "arena"
if _arena.is_dir():
    sys.path.insert(0, str(_arena))

from arena_runtime import ActionExecutor, ChallengeScope, ScopeDenied
from d2_badle import (
    BADLE_ARCHIVE_SHA256,
    BADLE_HARD_FLAG_SHA256,
    BADLE_HARD_LOG_SHA256,
    decode_badle_hard_log,
)


IDENTITY = {"id": 4, "name": "BadLE Hard", "category": "healthcare", "type": "standard"}
ARTIFACTS = {
    "APKlog.zip": (70_486_073, BADLE_ARCHIVE_SHA256),
    "cgm_ble_log.txt": (6_383, BADLE_HARD_LOG_SHA256),
}
ANALYZER = "d2-badle-hard-fallback-v1"
DECODE_ASSUMPTION = "documented_frame_6_selects_capture_index_4_by_range_oracle_offset_origin_unproven"


class ProfileError(ValueError):
    """Content-free failure suitable for the public result file."""


def validate_identity(challenge: dict[str, Any]) -> None:
    if not isinstance(challenge, dict):
        raise ProfileError("challenge_identity_mismatch")
    if type(challenge.get("id")) is not int or any(
        challenge.get(key) != value for key, value in IDENTITY.items()
    ):
        raise ProfileError("challenge_identity_mismatch")
    if any(challenge.get(field) for field in ("connection", "connectionInfo", "connection_info")):
        raise ProfileError("static_connection_forbidden")


def _origin(value: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or "\\" in value
            or any(ord(char) < 33 for char in value)
        ):
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), parsed.port or 443
    except (TypeError, ValueError):
        raise ProfileError("invalid_platform_url") from None


def validate_artifact_urls(challenge: dict[str, Any], base: str) -> dict[str, str]:
    validate_identity(challenge)
    origin = _origin(base)
    files = challenge.get("files")
    if not isinstance(files, list) or len(files) != len(ARTIFACTS):
        raise ProfileError("artifact_set_mismatch")
    urls: dict[str, str] = {}
    for value in files:
        if not isinstance(value, str):
            raise ProfileError("invalid_artifact_url")
        absolute = urllib.parse.urljoin(base.rstrip("/") + "/", value)
        if _origin(absolute) != origin:
            raise ProfileError("off_origin_artifact_url")
        encoded_name = urllib.parse.urlsplit(absolute).path.rsplit("/", 1)[-1]
        name = urllib.parse.unquote(encoded_name)
        if name != encoded_name or name not in ARTIFACTS or name in urls:
            raise ProfileError("artifact_name_mismatch")
        urls[name] = absolute
    if set(urls) != set(ARTIFACTS):
        raise ProfileError("artifact_set_mismatch")
    return urls


def _read_exact(path: Path, name: str) -> bytes:
    size, expected = ARTIFACTS[name]
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise ProfileError("artifact_size_or_type_mismatch")
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise ProfileError("artifact_digest_mismatch")
    return data


def _deny_shell(_command: str) -> str:
    raise ProfileError("shell_unavailable")


def solve_local(
    challenge: dict[str, Any],
    artifact_dir: str | Path,
    work_root: str | Path,
    submit_mode: str = "record",
    submit_flag: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate the two handouts, record a candidate, or call one bound callback."""
    started = time.perf_counter()
    report: dict[str, Any] = {
        **IDENTITY, "solved": False, "steps": 0, "solver": "deterministic",
        "candidate_found": False, "submission_attempted": False,
        "had_files": True, "had_instance": False,
        "submit_mode": submit_mode, "decode_assumption": DECODE_ASSUMPTION,
    }
    executor = None
    try:
        validate_identity(challenge)
        if submit_mode not in {"record", "submit"}:
            raise ProfileError("invalid_submit_mode")
        if submit_mode == "submit" and not callable(submit_flag):
            raise ProfileError("submit_callback_missing")
        source = Path(artifact_dir)
        if source.is_symlink() or not source.is_dir():
            raise ProfileError("artifact_directory_invalid")
        if {path.name for path in source.iterdir()} != set(ARTIFACTS):
            raise ProfileError("artifact_set_mismatch")
        root = Path(work_root)
        if root.is_symlink():
            raise ProfileError("work_root_invalid")
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "4"
        if destination.exists() or destination.is_symlink():
            raise ProfileError("work_directory_not_fresh")
        destination.mkdir(mode=0o700)
        for name in ARTIFACTS:
            data = _read_exact(source / name, name)
            with (destination / name).open("xb") as stream:
                stream.write(data)
        scope = ChallengeScope.from_controller(
            prompt="Decode only the exact BadLE Hard static handouts.",
            name=IDENTITY["name"], category=IDENTITY["category"],
            challenge_type=IDENTITY["type"], work_root=root,
            workdir=destination, filenames=tuple(ARTIFACTS), connection=None,
        )
        # This latch allows at most one outbound submission, even if the shared
        # executor asks again after a transport error or unknown verdict.
        def submit_once(flag: str) -> dict[str, str]:
            if report["submission_attempted"]:
                raise ProfileError("submission_already_attempted")
            report["submission_attempted"] = True
            try:
                reply = submit_flag(flag)
                status = str(reply.get("status", "error")).lower()
            except Exception:
                status = "error"
            if status not in {"correct", "already_solved", "incorrect"}:
                status = "error"
            report["verdict"] = {"status": status}
            return {"status": status}

        executor = ActionExecutor(
            scope, _deny_shell, submit_once, submit_mode=submit_mode,
            sleep=lambda _seconds: None,
        )
        executor.record_event("lifecycle", "ok", "badle_hard_static_fallback_started")
        executor.set_step(1)
        for name in ARTIFACTS:
            _read_exact(scope.require_path(destination / name), name)
        log_data = _read_exact(scope.require_path(destination / "cgm_ble_log.txt"), "cgm_ble_log.txt")
        extraction = decode_badle_hard_log(log_data)
        candidate_digest = hashlib.sha256(extraction.flag.encode("ascii")).hexdigest()
        if not hmac.compare_digest(candidate_digest, BADLE_HARD_FLAG_SHA256):
            raise ProfileError("candidate_regression_mismatch")
        report.update(steps=1, candidate_found=True, candidate_sha256=candidate_digest)
        combined_digest = hashlib.sha256(
            bytes.fromhex(BADLE_ARCHIVE_SHA256) + bytes.fromhex(BADLE_HARD_LOG_SHA256)
        ).hexdigest()
        outcome = executor.record_deterministic_candidate(
            flag=extraction.flag, analyzer=ANALYZER, artifact_sha256=combined_digest,
        )
        report["solved"] = bool(outcome.solved)
        if submit_mode == "record":
            report["final"] = "candidate_recorded"
        else:
            verdict = report.get("verdict", {}).get("status")
            report["final"] = {
                "correct": "platform_confirmed",
                "already_solved": "account_already_solved",
                "incorrect": "candidate_rejected",
                "error": "submission_unconfirmed",
            }.get(verdict, "submission_unconfirmed")
    except (ProfileError, ScopeDenied) as exc:
        code = str(exc)
        report["error"] = code if code.isascii() and all(c.isalnum() or c == "_" for c in code) else "profile_rejected"
        report["final"] = "submission_unconfirmed" if report["submission_attempted"] else "profile_rejected"
        if executor is not None:
            executor.record_event("lifecycle", "error", report["error"])
    except Exception:
        report["error"] = "local_processing_failed"
        if report["submission_attempted"]:
            verdict = report.get("verdict", {}).get("status")
            report["solved"] = verdict in {"correct", "already_solved"}
            report["final"] = {
                "correct": "platform_confirmed",
                "already_solved": "account_already_solved",
                "incorrect": "candidate_rejected",
            }.get(verdict, "submission_unconfirmed")
        else:
            report["final"] = "profile_rejected"
    finally:
        if executor is not None:
            executor.close()
        report["seconds"] = round(time.perf_counter() - started, 6)
    return report


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProfileError("artifact_redirect_denied")


def _download(url: str, path: Path, name: str, token: str, user_agent: str) -> None:
    size, digest = ARTIFACTS[name]
    request = urllib.request.Request(url, headers={
        "Authorization": "Token " + token,
        "User-Agent": user_agent, "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    hasher = hashlib.sha256()
    total = 0
    with opener.open(request, timeout=120) as response, path.open("xb") as output:
        if response.geturl() != url:
            raise ProfileError("artifact_redirect_denied")
        while True:
            block = response.read(min(65_536, size + 1 - total))
            if not block:
                break
            total += len(block)
            if total > size:
                raise ProfileError("artifact_size_mismatch")
            hasher.update(block)
            output.write(block)
    if total != size or not hmac.compare_digest(hasher.hexdigest(), digest):
        raise ProfileError("artifact_digest_mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-dir", type=Path, help="Local handouts; always record-only, never connects.")
    parser.add_argument("--work-root", type=Path, default=Path("/work"))
    args = parser.parse_args(argv)
    root = args.work_root
    if root.is_symlink():
        print("work_root_invalid", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    results_path = root / "results.json"
    if results_path.exists() or results_path.is_symlink():
        print("results_file_already_exists", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        if args.replay_dir is not None:
            result = solve_local(dict(IDENTITY), args.replay_dir, root, submit_mode="record")
            run_mode = "offline_replay"
        else:
            from ctfd import CTFdClient, UA
            base = os.environ.get("CTF_BASE") or os.environ.get("CTFD_URL") or ""
            token = os.environ.get("CTF_TOKEN") or os.environ.get("CTFD_TOKEN") or ""
            _origin(base)
            if not token:
                raise ProfileError("arena_token_missing")
            mode = os.environ.get("ARENA_SUBMIT_MODE", "record")
            if mode not in {"record", "submit"}:
                raise ProfileError("invalid_submit_mode")
            client = CTFdClient(base, token)
            challenge = client.challenge(4)
            urls = validate_artifact_urls(challenge, base)
            intake = root / "badle-hard-intake"
            intake.mkdir(mode=0o700, exist_ok=False)
            for name, url in urls.items():
                time.sleep(1.0)
                _download(url, intake / name, name, token, UA)
            def submit(flag: str) -> dict[str, Any]:
                time.sleep(1.0)
                return client.submit(4, flag)
            result = solve_local(challenge, intake, root, submit_mode=mode, submit_flag=submit)
            run_mode = "arena_static_only"
    except Exception as exc:
        code = str(exc) if isinstance(exc, ProfileError) else "platform_intake_failed"
        result = {**IDENTITY, "solved": False, "steps": 0, "candidate_found": False,
                  "submission_attempted": False, "error": code, "final": "profile_rejected"}
        run_mode = "offline_replay" if args.replay_dir else "arena_static_only"
    envelope = {
        "profile": ANALYZER, "mode": run_mode,
        "attempted": 1, "solved": int(result["solved"]),
        "total_seconds": round(time.perf_counter() - started, 6), "results": [result],
    }
    with results_path.open("x", encoding="utf-8") as stream:
        json.dump(envelope, stream, indent=2)
        stream.write("\n")
    print(json.dumps(envelope, separators=(",", ":")))
    return 2 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
