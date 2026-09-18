"""Provision the shared read-only Python tool environment for sandboxes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TOOL_PACKAGES: tuple[str, ...] = (
    "pwntools",
    "requests",
    "pycryptodome",
    "z3-solver",
    "pyelftools",
    "virtualenv",
)


class ToolchainError(RuntimeError):
    """Raised when the general agent tool environment cannot be created."""


def default_tool_root() -> Path:
    configured = os.environ.get("INCYPHER_AGENT_TOOL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".cypher_bridge" / "agent-tools").resolve()


def ensure_tool_root(
    tool_root: str | Path | None = None,
    *,
    packages: tuple[str, ...] = DEFAULT_TOOL_PACKAGES,
    timeout: float = 1800.0,
) -> Path:
    """Create and populate a self-contained Python venv for agent bash tools.

    The result is intended to be read-only mounted into every worker sandbox.
    Agents can still create their own venv inside the workspace for extra
    dependencies.
    """

    root = Path(tool_root).expanduser().resolve() if tool_root else default_tool_root()
    python = root / "bin" / "python"
    ready = root / ".ready.json"
    if python.exists() and ready.exists():
        return root
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    system_python = (
        "/usr/bin/python3"
        if Path("/usr/bin/python3").exists()
        else shutil.which("python3") or sys.executable
    )
    uv = shutil.which("uv")
    try:
        if uv:
            create_command = [
                uv,
                "venv",
                "--python",
                system_python,
                "--seed",
                str(root),
            ]
        else:
            create_command = [
                system_python,
                "-m",
                "venv",
                "--copies",
                str(root),
            ]
        subprocess.run(
            create_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise ToolchainError(
            f"failed to create tool venv at {root}: {exc.stderr or exc.stdout}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError(f"tool venv creation timed out: {exc}") from exc

    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    if uv:
        command = [uv, "pip", "install", "--python", str(python), *packages]
    else:
        command = [str(python), "-m", "pip", "install", *packages]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise ToolchainError(
            f"failed to install tool packages into {root}: {exc.stderr or exc.stdout}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError(f"tool package installation timed out: {exc}") from exc

    ready.write_text(
        json.dumps(
            {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "python": str(python),
                "packages": list(packages),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root
