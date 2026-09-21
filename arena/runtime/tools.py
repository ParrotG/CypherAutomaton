"""Tool execution for the arena brain."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ACCEPTED_VERDICTS = {"correct", "already_solved"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a shell command in the challenge work directory. "
                "Commands time out after 120 seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_flag",
            "description": "Submit a candidate flag to the platform for verification.",
            "parameters": {
                "type": "object",
                "properties": {"flag": {"type": "string"}},
                "required": ["flag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_editor",
            "description": (
                "View or edit files inside the challenge work directory. "
                "Supported commands: view, create, str_replace, insert."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert"],
                    },
                    "path": {"type": "string"},
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
]


@dataclass
class ToolOutcome:
    content: str
    solved: bool = False
    flag: str | None = None
    verdict: dict[str, Any] | None = None


def normalize_flag(raw_flag: str) -> str:
    raw = raw_flag.strip()
    match = re.fullmatch(r"(?:[A-Za-z0-9_.-]+)\{([^}\r\n]*)\}", raw)
    body = match.group(1).strip() if match else raw
    if not body or any(char in body for char in "{}"):
        raise ValueError("could not extract a valid flag body")
    return f"INCYPHER{{{body}}}"


class ToolExecutor:
    def __init__(
        self,
        *,
        run_bash: Callable[[str], str],
        submit_flag: Callable[[str], dict[str, Any]],
        workspace_dir: str | Path | None = None,
        max_output: int = 20_000,
    ) -> None:
        self.run_bash = run_bash
        self.submit_flag = submit_flag
        self.workspace = Path(workspace_dir).resolve() if workspace_dir else None
        self.max_output = max(1, int(max_output))

    def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolOutcome:
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
            if name == "run_bash":
                return self._run_bash(args)
            if name == "submit_flag":
                return self._submit_flag(args)
            if name == "str_replace_editor":
                return self._editor(args)
            return ToolOutcome(content=json.dumps({"error": f"unknown tool: {name}"}))
        except Exception as exc:  # tools return errors to the model
            return ToolOutcome(content=json.dumps({"error": f"{type(exc).__name__}: {exc}"}))

    def _run_bash(self, args: dict[str, Any]) -> ToolOutcome:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolOutcome(content=json.dumps({"error": "empty command"}))
        output = str(self.run_bash(command) or "")
        truncated = len(output) > self.max_output
        if truncated:
            half = self.max_output // 2
            output = output[:half] + "\n...[TRUNCATED]...\n" + output[-half:]
        return ToolOutcome(content=output)

    def _submit_flag(self, args: dict[str, Any]) -> ToolOutcome:
        raw_flag = str(args.get("flag", "")).strip()
        if not raw_flag:
            return ToolOutcome(content=json.dumps({"error": "flag is required"}))
        normalized = normalize_flag(raw_flag)
        verdict = self.submit_flag(normalized)
        if not isinstance(verdict, dict):
            verdict = {"status": str(verdict)}
        solved = str(verdict.get("status", "")).lower() in ACCEPTED_VERDICTS
        return ToolOutcome(
            content=json.dumps({"submitted": normalized, "verdict": verdict}, ensure_ascii=False),
            solved=solved,
            flag=normalized,
            verdict=verdict,
        )

    # ------------------------------------------------------------------
    # Host-side editor
    # ------------------------------------------------------------------
    def _resolve_path(self, raw_path: str) -> Path:
        if self.workspace is None:
            raise ValueError("editor is unavailable because no workspace_dir was supplied")
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
        else:
            numbered = [f"{idx:>6}\t{line}" for idx, line in enumerate(lines, start=1)]
        body = "\n".join(numbered)
        if len(body) > self.max_output:
            body = body[: self.max_output] + "\n...[TRUNCATED]..."
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
