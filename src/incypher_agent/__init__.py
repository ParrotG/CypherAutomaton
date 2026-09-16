"""Minimal autonomous CTF agent unit for IN-CYPHER raw-TCP challenges.

The unit is deliberately a small model-tool loop, not a framework.  It keeps a
free workspace, uses a minimal tool set, persists an event log, and enforces
only hard limits in this phase.
"""

from .config import AgentConfig, AgentConfigError
from .loop import AgentLoop, AgentResult

__all__ = ["AgentConfig", "AgentConfigError", "AgentLoop", "AgentResult"]
