"""Compatibility entrypoint for the official /opt/agent/main.py runner.

The official base image declares ``ENTRYPOINT ["python", "/opt/agent/main.py"]``.
Some arena runners invoke that path directly instead of honoring the image
ENTRYPOINT.  This shim makes both invocation styles run the arena scheduler.
"""

from __future__ import annotations

from arena.main import main


if __name__ == "__main__":
    raise SystemExit(main())
