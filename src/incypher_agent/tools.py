"""Minimal tool set for the phase-A agent unit: bash + editor + report_flag."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig
from .state import write_tool_artifact


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a bash command in the agent workspace. Use this for all "
                "exploration and script execution. The command runs with the "
                "workspace as cwd and has a timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to execute.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Optional timeout in seconds.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_editor",
            "description": (
                "View or edit files inside the agent workspace. Supported "
                "commands: view, create, str_replace, insert."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert"],
                    },
                    "path": {
                        "type": "string",
                        "description": "File path, relative to the workspace or absolute inside it.",
                    },
                    "file_text": {"type": "string"},
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "insert_line": {"type": "integer"},
                },
                "required": ["command", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_flag",
            "description": (
                "Record a candidate challenge flag and stop the run successfully. "
                "Use this when you have extracted a flag and verified it as much "
                "as the environment allows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flag": {
                        "type": "string",
                        "description": "The flag string, e.g. flag{...} or INCYPHER{...}.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Short explanation of how the flag was obtained.",
                    },
                },
                "required": ["flag"],
            },
        },
    },
]


@dataclass
class ToolOutcome:
    content: str
    done: bool = False
    status: str | None = None
    flag: str | None = None


class ToolExecutor:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.workspace = Path(config.workspace_dir).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def execute(self, name: str, arguments: str | dict[str, Any], *, tool_call_id: str = "") -> ToolOutcome:
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return ToolOutcome(content=json.dumps({"error": f"invalid JSON arguments: {exc}"}))
        else:
            args = arguments or {}
        if not isinstance(args, dict):
            return ToolOutcome(content=json.dumps({"error": "arguments must be an object"}))

        try:
            if name == "bash":
                return self._bash(args, tool_call_id=tool_call_id)
            if name == "str_replace_editor":
                return self._editor(args)
            if name == "report_flag":
                return self._report_flag(args)
            return ToolOutcome(content=json.dumps({"error": f"unknown tool: {name}"}))
        except Exception as exc:  # noqa: BLE001 - tools must return errors to the model
            return ToolOutcome(content=json.dumps({"error": f"{type(exc).__name__}: {exc}"}))

    # ------------------------------------------------------------------
    # bash
    # ------------------------------------------------------------------
    def _bash(self, args: dict[str, Any], *, tool_call_id: str) -> ToolOutcome:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolOutcome(content=json.dumps({"error": "empty command"}))
        timeout = float(args.get("timeout") or self.config.bash_timeout)
        timeout = max(0.1, min(timeout, 3600.0))

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONUNBUFFERED": "1",
        }
        started = time.monotonic()
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            cwd=str(self.workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            stdout_bytes, stderr_bytes = process.communicate()
        elapsed = time.monotonic() - started

        stdout_path = write_tool_artifact(
            self.config.run_dir / "agent",
            tool_call_id=tool_call_id or "bash",
            direction="stdout",
            data=stdout_bytes,
        )
        stderr_path = write_tool_artifact(
            self.config.run_dir / "agent",
            tool_call_id=tool_call_id or "bash",
            direction="stderr",
            data=stderr_bytes,
        )

        stdout_text, out_truncated = self._truncate(stdout_bytes)
        stderr_text, err_truncated = self._truncate(stderr_bytes)
        payload = {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_seconds": round(elapsed, 3),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": out_truncated,
            "stderr_truncated": err_truncated,
            "stdout_artifact": str(stdout_path),
            "stderr_artifact": str(stderr_path),
        }
        return ToolOutcome(content=json.dumps(payload, ensure_ascii=False))

    # ------------------------------------------------------------------
    # str_replace_editor
    # ------------------------------------------------------------------
    def _editor(self, args: dict[str, Any]) -> ToolOutcome:
        command = str(args.get("command", "")).strip()
        path = self._resolve_path(str(args.get("path", "")))
        if command == "view":
            return ToolOutcome(content=self._view(path, args))
        if command == "create":
            return ToolOutcome(content=self._create(path, args))
        if command == "str_replace":
            return ToolOutcome(content=self._str_replace(path, args))
        if command == "insert":
            return ToolOutcome(content=self._insert(path, args))
        return ToolOutcome(content=json.dumps({"error": f"unsupported editor command: {command}"}))

    def _resolve_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("path is required")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {raw_path}") from exc
        return resolved

    def _view(self, path: Path, args: dict[str, Any]) -> str:
        if not path.exists() or not path.is_file():
            return json.dumps({"error": f"file does not exist: {path}"})
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        view_range = args.get("view_range")
        if isinstance(view_range, list) and len(view_range) == 2:
            start, end = int(view_range[0]), int(view_range[1])
            start = max(1, start)
            end = min(len(lines), end)
            selected = lines[start - 1 : end]
            numbered = [f"{idx:>6}\t{line}" for idx, line in enumerate(selected, start=start)]
            body = "\n".join(numbered)
            if len(body) > self.config.max_tool_output:
                body = body[: self.config.max_tool_output] + "\n...[TRUNCATED]..."
            return body
        numbered = [f"{idx:>6}\t{line}" for idx, line in enumerate(lines, start=1)]
        body = "\n".join(numbered)
        if len(body) > self.config.max_tool_output:
            body = body[: self.config.max_tool_output] + "\n...[TRUNCATED]..."
        return body

    def _create(self, path: Path, args: dict[str, Any]) -> str:
        if path.exists():
            return json.dumps({"error": f"file already exists: {path}"})
        file_text = args.get("file_text")
        if not isinstance(file_text, str):
            return json.dumps({"error": "file_text is required for create"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_text, encoding="utf-8")
        return json.dumps({"path": str(path), "created": True, "bytes": len(file_text.encode("utf-8"))})

    def _str_replace(self, path: Path, args: dict[str, Any]) -> str:
        if not path.exists() or not path.is_file():
            return json.dumps({"error": f"file does not exist: {path}"})
        old_str = args.get("old_str")
        new_str = args.get("new_str")
        if not isinstance(old_str, str) or not isinstance(new_str, str):
            return json.dumps({"error": "old_str and new_str are required"})
        text = path.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count != 1:
            return json.dumps({"error": f"old_str must occur exactly once (found {count})"})
        updated = text.replace(old_str, new_str, 1)
        path.write_text(updated, encoding="utf-8")
        return json.dumps({"path": str(path), "replaced": 1, "bytes": len(updated.encode("utf-8"))})

    def _insert(self, path: Path, args: dict[str, Any]) -> str:
        if not path.exists() or not path.is_file():
            return json.dumps({"error": f"file does not exist: {path}"})
        new_str = args.get("new_str")
        if not isinstance(new_str, str):
            return json.dumps({"error": "new_str is required"})
        raw_line = args.get("insert_line")
        if not isinstance(raw_line, int):
            return json.dumps({"error": "insert_line is required"})
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        index = max(0, min(raw_line, len(lines)))
        lines.insert(index, new_str)
        path.write_text("".join(lines), encoding="utf-8")
        return json.dumps({"path": str(path), "inserted_at": index})

    # ------------------------------------------------------------------
    # report_flag
    # ------------------------------------------------------------------
    def _report_flag(self, args: dict[str, Any]) -> ToolOutcome:
        flag = str(args.get("flag", "")).strip()
        evidence = str(args.get("evidence", "")).strip()
        if not flag:
            return ToolOutcome(content=json.dumps({"error": "flag is required"}))
        if self.config.flag_pattern:
            try:
                if not re.search(self.config.flag_pattern, flag):
                    return ToolOutcome(
                        content=json.dumps(
                            {
                                "error": "flag does not match the configured flag_pattern",
                                "flag_pattern": self.config.flag_pattern,
                                "flag": flag,
                            }
                        )
                    )
            except re.error as exc:
                return ToolOutcome(content=json.dumps({"error": f"invalid flag_pattern: {exc}"}))
        return ToolOutcome(
            content=json.dumps({"status": "FLAG_REPORTED", "flag": flag, "evidence": evidence}),
            done=True,
            status="FLAG_REPORTED",
            flag=flag,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _truncate(self, data: bytes) -> tuple[str, bool]:
        text = data.decode("utf-8", errors="replace")
        limit = max(1, int(self.config.max_tool_output))
        if len(text) <= limit:
            return text, False
        head = limit // 2
        tail = limit - head
        return text[:head] + "\n...[TRUNCATED]...\n" + text[-tail:], True
