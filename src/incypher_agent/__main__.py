"""Allow ``python -m incypher_agent ...``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
