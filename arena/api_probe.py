"""Read-only live probe for the official CTF agent API."""

from __future__ import annotations

import json

from .platform import connect_from_env


def main() -> int:
    client = connect_from_env()
    me = client.me()
    challenges = client.list_challenges()
    solved = set(me.get("solved") or [])
    summary = {
        "team_id": me.get("team_id"),
        "solved_count": len(solved),
        "challenge_count": len(challenges),
        "challenges": [
            {
                "id": int(ch["id"]),
                "name": ch.get("name"),
                "category": ch.get("category"),
                "type": ch.get("type"),
                "points": ch.get("points"),
                "practice": "practice" in str(ch.get("category") or "").lower(),
                "solved": int(ch["id"]) in solved,
            }
            for ch in challenges
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
