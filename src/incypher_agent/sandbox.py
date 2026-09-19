"""Sandboxed command execution for the minimal agent unit.

The default backend is bubblewrap (bwrap).  It gives each agent a private PID,
UTS and IPC namespace, a private /tmp, and a workspace-only writable mount,
without requiring a container daemon.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    """Raised when a sandbox backend cannot be configured."""


@dataclass(frozen=True)
class SandboxCommand:
    argv: list[str]
    cwd: str | None
    env: dict[str, str]
    backend: str


class LocalSandbox:
    """Run commands directly on the host (development / fallback only)."""

    name = "local"

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def prepare(self, command: str) -> SandboxCommand:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONUNBUFFERED": "1",
        }
        return SandboxCommand(
            argv=["/bin/bash", "-lc", command],
            cwd=str(self.workspace),
            env=env,
            backend=self.name,
        )


class BwrapSandbox:
    """Run every bash command inside a fresh bubblewrap namespace."""

    name = "bwrap"

    def __init__(
        self,
        workspace: Path,
        *,
        bwrap_path: str = "bwrap",
        tool_root: str | Path | None = None,
        network: bool = True,
        blackboard_dir: str | Path | None = None,
        blackboard_path: str = "/blackboard",
        worker_id: str = "worker",
    ) -> None:
        resolved = shutil.which(bwrap_path)
        if not resolved:
            raise SandboxError(f"bwrap executable not found: {bwrap_path!r}")
        self.bwrap = resolved
        self.workspace = workspace
        self.tool_root = (
            Path(tool_root).expanduser().resolve() if tool_root else None
        )
        self.network = network
        self.blackboard_dir = (
            Path(blackboard_dir).expanduser().resolve()
            if blackboard_dir
            else None
        )
        self.blackboard_path = blackboard_path
        self.worker_id = worker_id

    def prepare(self, command: str) -> SandboxCommand:
        args = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
        ]
        if not self.network:
            args.append("--unshare-net")
        for path in ("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc"):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])
        if self.tool_root is not None and self.tool_root.exists():
            args.extend(["--ro-bind", str(self.tool_root), str(self.tool_root)])
        args.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(self.workspace),
                "/workspace",
            ]
        )
        helper = Path(__file__).with_name("blackboard_cli.py")
        if helper.exists():
            for name in ("bb-write", "bb-read"):
                args.extend(
                    ["--ro-bind", str(helper), f"/tmp/{name}"]
                )
        if self.blackboard_dir is not None:
            self.blackboard_dir.mkdir(parents=True, exist_ok=True)
            args.extend(
                ["--bind", str(self.blackboard_dir), self.blackboard_path]
            )
        args.extend(
            [
                "--chdir",
                "/workspace",
                "--clearenv",
            ]
        )
        tool_bin = (
            f"{self.tool_root}/bin"
            if self.tool_root is not None and self.tool_root.exists()
            else ""
        )
        sandbox_path = ":".join(
            part
            for part in (tool_bin, "/tmp", "/usr/local/bin", "/usr/bin", "/bin")
            if part
        )
        env = {
            "HOME": "/workspace",
            "TMPDIR": "/tmp",
            "PATH": sandbox_path,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONUNBUFFERED": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
        env["INCYPHER_WORKER_ID"] = self.worker_id
        if self.blackboard_dir is not None:
            env["INCYPHER_BLACKBOARD"] = self.blackboard_path
        for key, value in env.items():
            args.extend(["--setenv", key, value])
        args.extend(["--", "/bin/bash", "-lc", command])
        return SandboxCommand(
            argv=args,
            cwd=str(self.workspace),
            env={},
            backend=self.name,
        )


def create_sandbox(config: Any) -> LocalSandbox | BwrapSandbox:
    """Create the configured sandbox backend for *config*."""

    backend = str(getattr(config, "sandbox_backend", "bwrap")).strip().lower()
    workspace = Path(config.workspace_dir).resolve()
    if backend == "local":
        return LocalSandbox(workspace)
    if backend == "bwrap":
        configured_tool_root = getattr(config, "sandbox_tool_root", None)
        if not configured_tool_root:
            from .toolchain import default_tool_root

            candidate = default_tool_root()
            configured_tool_root = str(candidate) if candidate.exists() else None
        return BwrapSandbox(
            workspace,
            bwrap_path=getattr(config, "sandbox_bwrap", "bwrap"),
            tool_root=configured_tool_root,
            network=bool(getattr(config, "sandbox_network", True)),
            blackboard_dir=getattr(config, "blackboard_dir", None),
            blackboard_path=getattr(config, "blackboard_path", "/blackboard"),
            worker_id=str(getattr(config, "run_id", "worker")),
        )
    raise SandboxError(f"unknown sandbox backend: {backend!r}")
