"""Create an allowlisted source handoff; never builds, pushes or submits."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "deploy/badle_hard/entrypoint.py",
    "deploy/badle_hard/Dockerfile",
    "deploy/badle_hard/Dockerfile.dockerignore",
    "deploy/arena/arena_runtime.py",
    "deploy/arena/d2_badle.py",
    "tests/test_badle_hard_submission.py",
    "tests/test_d2_badle.py",
    "docs/BADLE_HARD_SUBMISSION.md",
    "docs/solutions/badle-hard.md",
    "scripts/package_badle_hard.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contents = {}
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Missing or non-regular allowlisted source file: " + name)
        contents[name] = path.read_bytes()
    destination = args.output.resolve()
    if not destination.is_relative_to(ROOT) or destination == ROOT:
        raise SystemExit("Output must be a new child directory of the checkout.")
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination / "badle-hard-source.zip"
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    manifest = {
        "profile": "badle-hard-static-only",
        "challenge_id": 4,
        "platform_acceptance": "unverified",
        "decode_assumption": "documented_frame_6_selects_capture_index_4_by_range_oracle_offset_origin_unproven",
        "archive": archive_path.name,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in contents.items()],
        "excluded": ["credentials", "handouts", "candidate_plaintext", "run_evidence", "other_solvers"],
    }
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"output": str(destination), "files": len(contents),
                      "archive_sha256": manifest["archive_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
