"""Temporary IN-CYPHER bridge: PoW gate + local maintainable TCP session.

The project is intentionally independent of third-party packages so it can run
inside a restricted sandbox/agent container.  ``solver.py`` at the repository
root provides an official-helper-compatible ``connect`` function for direct
use; the bridge daemon adds a local, human-inspectable TCP proxy around that
connection.
"""

from .pow import PowChallenge, PowError, PowRejected, PowVariant, solve_pow
from .client import PrefixedSocket, connect

__all__ = [
    "PowChallenge",
    "PowError",
    "PowRejected",
    "PowVariant",
    "PrefixedSocket",
    "connect",
    "solve_pow",
]
