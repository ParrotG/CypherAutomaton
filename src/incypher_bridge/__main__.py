"""Allow ``python -m incypher_bridge ...``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
