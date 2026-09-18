from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--agent-endpoint", default="")
    parser.add_argument("--run-id", default="")
    args, _ = parser.parse_known_args()
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "fake_worker.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "agent_endpoint": args.agent_endpoint,
                "argv": sys.argv,
            }
        ),
        encoding="utf-8",
    )
    return int(os.environ.get("FAKE_SCHEDULER_EXIT_CODE", "10"))


if __name__ == "__main__":
    raise SystemExit(main())
