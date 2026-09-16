#!/usr/bin/env python3
"""Project-root wrapper around ``python -m incypher_bridge``."""

from incypher_bridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
