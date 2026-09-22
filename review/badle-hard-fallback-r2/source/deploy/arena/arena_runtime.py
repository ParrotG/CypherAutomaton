"""Deterministic runtime boundary for the IN-CYPHER arena Brain.

The model emits one JSON action at a time.  This module parses the organizer
prompt into a sealed scope, validates each action, owns all network sessions,
keeps secrets out of model messages and subprocesses, and records only redacted
action metadata.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import gzip
import hashlib
from html.parser import HTMLParser
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import struct
import tarfile
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request
import zipfile


FLAG_RE = re.compile(r"(?:INCYPHER|flag)\{[^{}\r\n]{1,200}\}")
TEAM_KEY_RE = re.compile(r"(?i)team_key=([A-Za-z0-9_.:-]{8,256})")
TOKEN_RE = re.compile(r"(?i)\bctfd_[A-Za-z0-9._-]{12,}\b")
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(API_KEY|LLM_API_KEY|CTF_TOKEN|CTFD_TOKEN)\s*=\s*[^\s]+"
)
MAX_PROMPT_CHARS = 50_000
MAX_OBSERVATION_CHARS = 65_536
MAX_CUMULATIVE_OBSERVATION_CHARS = 524_288
MAX_HTTP_REQUESTS = 20
MAX_SUBMISSIONS = 3
MAX_PWN_CONNECTIONS = 2
MAX_PWN_CONNECT_ATTEMPTS = 2
MAX_PWN_RECEIVES = 16
MAX_TCP_TEXT_REPLIES = 8
MAX_TCP_TEXT_REPLY_BYTES = 256
MAX_TCP_TEXT_TOTAL_BYTES = 2_048
MAX_ORACLE_PWN_RECEIVES = 128
MAX_ORACLE_TCP_TEXT_REPLIES = 60
MAX_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_MEMBERS = 128
MAX_ZIP_CENTRAL_BYTES = 4 * 1024 * 1024
MAX_INVENTORY_HASH_BYTES = 128 * 1024 * 1024
PLATFORM_HOSTS = {
    "registry.in-cypher.com",
    "hackathonlive.in-cypher.com",
    "hackathon.in-cypher.com",
}
WEB_PRACTICE_IDENTITIES = {
    ("19", "Mustache Trap", "(practice) web", "dynamic_iac"): "mustache",
    ("24", "Schema Ghost", "(practice) web", "dynamic_iac"): "schema_ghost",
    ("42", "Parcelport", "(practice) web", "dynamic_iac"): "parcelport",
    ("68", "SSTI", "(practice) web", "dynamic_iac"): "ssti",
}
ORACLE_PRACTICE_IDENTITY = (
    "33",
    "Oracle's Riddle",
    "(practice) misc",
    "dynamic_iac",
)
DESTRUCTIVE_WORD_RE = re.compile(
    r"(?i)(delete|destroy|drop|purge|remove|reset|revoke|shutdown|terminate|wipe)"
)


class ScopeDenied(ValueError):
    """An action did not fit the sealed challenge scope."""


class ModelFailure(RuntimeError):
    """Content-free provider failure safe for results and evidence logs."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        attempts: int,
        status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        self.status = status


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normal_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ScopeDenied("invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise ScopeDenied("url_userinfo_forbidden")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ScopeDenied("invalid_url_port") from exc
    raw_host = parsed.hostname.rstrip(".").lower()
    try:
        host = ipaddress.ip_address(raw_host).compressed
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ScopeDenied("invalid_url_host") from exc
    default = 80 if parsed.scheme == "http" else 443
    suffix = "" if port in (None, default) else f":{port}"
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{rendered_host}{suffix}"


def _normal_scoped_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ScopeDenied("invalid_url")
    parsed = urllib.parse.urlsplit(value)
    if parsed.fragment:
        raise ScopeDenied("url_fragment_forbidden")
    origin = _normal_origin(value)
    decoded_path = _fully_unquote(parsed.path or "/")
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise ScopeDenied("url_path_traversal")
    path = urllib.parse.quote(decoded_path, safe="/%:@!$&'()*+,;=-._~")
    return origin + path + (("?" + parsed.query) if parsed.query else "")


def _seal_workdir(
    work_root: Path, workdir_value: str | Path, filenames: Sequence[str]
) -> tuple[Path, tuple[Path, ...]]:
    workdir = Path(workdir_value).resolve()
    try:
        info = workdir.lstat()
    except OSError as exc:
        raise ScopeDenied("workdir_unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ScopeDenied("unsafe_workdir")
    if workdir.parent != work_root or not workdir.name.isdecimal():
        raise ScopeDenied("workdir_outside_controller_root")
    files: list[Path] = []
    for name_value in filenames:
        if not isinstance(name_value, str) or not name_value or name_value in {".", ".."}:
            raise ScopeDenied("invalid_artifact_name")
        if Path(name_value).name != name_value or "/" in name_value or "\\" in name_value:
            raise ScopeDenied("artifact_path_escape")
        candidate = workdir / name_value
        try:
            file_info = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ScopeDenied("artifact_unavailable") from exc
        if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode):
            raise ScopeDenied("unsafe_artifact")
        if not _is_relative_to(resolved, workdir):
            raise ScopeDenied("artifact_path_escape")
        files.append(resolved)
    return workdir, tuple(files)


def _connection_scope(
    connection: str,
) -> tuple[str | None, str | None, str | None, int | None, str | None]:
    origin = None
    entry_url = None
    url_match = re.search(r"https?://[^\s<>()`]+", connection)
    if url_match:
        raw_url = url_match.group(0).rstrip(".,;]")
        entry_url = _normal_scoped_url(raw_url)
        origin = _normal_origin(entry_url)
        if urllib.parse.urlsplit(origin).hostname.rstrip(".") in PLATFORM_HOSTS:
            raise ScopeDenied("platform_origin_forbidden")

    pwn_host = None
    pwn_port = None
    pwn_match = re.search(r"(?m)^\s*nc\s+([^\s]+)\s+(\d{1,5})\b", connection)
    if pwn_match:
        host = pwn_match.group(1).strip().lower().rstrip(".")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ScopeDenied("invalid_pwn_host")
        port = int(pwn_match.group(2))
        if not 1 <= port <= 65535:
            raise ScopeDenied("invalid_pwn_port")
        if host in PLATFORM_HOSTS:
            raise ScopeDenied("platform_host_forbidden")
        pwn_host, pwn_port = host, port

    key_match = TEAM_KEY_RE.search(connection)
    team_key = key_match.group(1) if key_match else None
    return origin, entry_url, pwn_host, pwn_port, team_key


def _sanitize_prompt(prompt: str, team_key: str | None) -> str:
    value = prompt
    if team_key:
        value = value.replace(team_key, "<redacted-team-key>")
    value = TOKEN_RE.sub("<redacted-token>", value)
    value = ASSIGNMENT_SECRET_RE.sub(
        lambda match: match.group(1) + "=<redacted>", value
    )
    return value


def _fully_unquote(value: str) -> str:
    current = value
    for _ in range(32):
        decoded = urllib.parse.unquote_plus(current)
        if decoded == current:
            return current
        current = decoded
    raise ScopeDenied("nested_encoding_exhausted")


def _http_path(url: str) -> str:
    return urllib.parse.urlsplit(_normal_scoped_url(url)).path or "/"


def _destructive_text(value: str) -> bool:
    return DESTRUCTIVE_WORD_RE.search(_fully_unquote(value)) is not None


@dataclass(frozen=True)
class ChallengeScope:
    name: str
    category: str
    challenge_type: str
    work_root: Path
    workdir: Path | None
    files: tuple[Path, ...]
    origin: str | None
    entry_url: str | None
    pwn_host: str | None
    pwn_port: int | None
    team_key: str | None = field(repr=False)
    allow_tcp_dialogue: bool
    web_practice: str | None
    model_prompt: str

    @classmethod
    def from_prompt(
        cls, prompt: str, *, work_root: str | Path | None = None
    ) -> "ChallengeScope":
        if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise ScopeDenied("invalid_prompt")
        if work_root is None:
            raise ScopeDenied("unsealed_prompt_scope")
        root = Path(work_root).resolve()
        name_matches = re.findall(r"(?m)^# Challenge:\s*(.+?)\s*$", prompt)
        metadata_matches = re.findall(
            r"(?m)^Category:\s*(.*?)\s+Points:.*?\s+Type:\s*(\S+)\s*$", prompt
        )
        file_headings = re.findall(r"(?m)^## Files \(in .+?/\)\s*$", prompt)
        connection_headings = re.findall(
            r"(?m)^## Live instance connection info\s*$", prompt
        )
        if len(name_matches) != 1 or len(metadata_matches) != 1:
            raise ScopeDenied("prompt_metadata_missing")
        if len(file_headings) > 1 or len(connection_headings) > 1:
            raise ScopeDenied("duplicate_controller_section")
        name = name_matches[0].strip()[:200]
        category = metadata_matches[0][0].strip().lower()[:80]
        challenge_type = metadata_matches[0][1].strip()[:80]

        workdir: Path | None = None
        files: list[Path] = []
        section = re.search(
            r"(?ms)^## Files \(in (.+?)/\)\s*\n(.*?)(?=^## |\Z)", prompt
        )
        if section:
            names = []
            for raw in section.group(2).splitlines():
                if not raw.startswith("- "):
                    continue
                name_value = raw[2:].strip()
                if " (download failed:" in name_value:
                    continue
                names.append(name_value)
            workdir, sealed = _seal_workdir(root, section.group(1), names)
            files.extend(sealed)

        connection = ""
        conn_match = re.search(
            r"(?ms)^## Live instance connection info\s*\n(.*?)(?=^## |\Z)", prompt
        )
        if conn_match:
            connection = conn_match.group(1)

        origin, entry_url, pwn_host, pwn_port, team_key = _connection_scope(connection)
        return cls(
            name=name,
            category=category,
            challenge_type=challenge_type,
            work_root=root,
            workdir=workdir,
            files=tuple(files),
            origin=origin,
            entry_url=entry_url,
            pwn_host=pwn_host,
            pwn_port=pwn_port,
            team_key=team_key,
            allow_tcp_dialogue=False,
            web_practice=None,
            model_prompt=_sanitize_prompt(prompt, team_key),
        )

    @classmethod
    def from_controller(
        cls,
        *,
        prompt: str,
        name: str,
        category: str,
        challenge_type: str,
        work_root: str | Path,
        workdir: str | Path,
        filenames: Sequence[str],
        connection: str | None,
        allow_tcp_dialogue: bool = False,
        web_practice: str | None = None,
    ) -> "ChallengeScope":
        """Build scope from trusted harness fields, never from description text."""
        if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise ScopeDenied("invalid_prompt")
        root = Path(work_root).resolve()
        sealed_dir, files = _seal_workdir(root, workdir, filenames)
        connection_value = connection or ""
        origin, entry_url, pwn_host, pwn_port, team_key = _connection_scope(
            connection_value
        )
        if str(challenge_type) != "dynamic_iac" and any(
            value is not None
            for value in (origin, entry_url, pwn_host, pwn_port, team_key)
        ):
            raise ScopeDenied("static_challenge_connection_forbidden")
        if not isinstance(allow_tcp_dialogue, bool):
            raise ScopeDenied("tcp_dialogue_capability_invalid")
        if allow_tcp_dialogue and (
            str(challenge_type) != "dynamic_iac"
            or files
            or not pwn_host
            or not pwn_port
            or not team_key
        ):
            raise ScopeDenied("tcp_dialogue_scope_invalid")
        web_identity = (
            sealed_dir.name,
            str(name).strip()[:200],
            str(category).strip().lower()[:80],
            str(challenge_type).strip()[:80],
        )
        expected_web_practice = WEB_PRACTICE_IDENTITIES.get(web_identity)
        if web_practice is not None and (
            web_practice != expected_web_practice
            or str(challenge_type) != "dynamic_iac"
            or files
            or not origin
            or not entry_url
            or pwn_host is not None
            or pwn_port is not None
            or team_key is not None
            or allow_tcp_dialogue
        ):
            raise ScopeDenied("web_practice_scope_invalid")
        return cls(
            name=str(name).strip()[:200],
            category=str(category).strip().lower()[:80],
            challenge_type=str(challenge_type).strip()[:80],
            work_root=root,
            workdir=sealed_dir,
            files=files,
            origin=origin,
            entry_url=entry_url,
            pwn_host=pwn_host,
            pwn_port=pwn_port,
            team_key=team_key,
            allow_tcp_dialogue=allow_tcp_dialogue,
            web_practice=web_practice,
            model_prompt=_sanitize_prompt(prompt, team_key),
        )

    def require_url(self, url: str) -> str:
        if self.origin is None:
            raise ScopeDenied("network_not_in_scope")
        normalized = _normal_scoped_url(url)
        if _normal_origin(normalized) != self.origin:
            raise ScopeDenied("off_origin_url")
        return normalized

    def require_path(self, value: str | Path) -> Path:
        try:
            path = Path(value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ScopeDenied("path_unavailable") from exc
        if path not in self.files:
            raise ScopeDenied("path_not_sealed")
        return path

    def scope_summary(self) -> dict[str, Any]:
        return {
            "challenge": self.name,
            "category": self.category,
            "type": self.challenge_type,
            "files": [str(path) for path in self.files],
            "origin": self.origin,
            "entry_url": self.entry_url,
            "pwn_endpoint": (
                f"{self.pwn_host}:{self.pwn_port}" if self.pwn_host else None
            ),
            "pwn_gate_available": bool(self.pwn_host and self.team_key),
            "tcp_dialogue_allowed": self.allow_tcp_dialogue,
            "web_practice": self.web_practice,
        }

    def instance_binding_sha256(self) -> str:
        material = "\0".join((
            self.name,
            str(self.workdir or ""),
            self.entry_url or "",
            self.pwn_host or "",
            str(self.pwn_port or ""),
            self.team_key or "",
            "1" if self.allow_tcp_dialogue else "0",
            self.web_practice or "",
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_oracle_practice_scope(scope: ChallengeScope) -> bool:
    """Return whether the controller sealed the exact Oracle practice profile."""

    return bool(
        scope.allow_tcp_dialogue
        and scope.workdir is not None
        and (
            scope.workdir.name,
            scope.name,
            scope.category,
            scope.challenge_type,
        )
        == ORACLE_PRACTICE_IDENTITY
    )


def extract_action(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("model_response_not_text")
    value = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    if fence:
        value = fence.group(1).strip()
    def strict_object(pairs):  # noqa: ANN001
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("model_action_duplicate_field")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=strict_object)
    try:
        parsed, end = decoder.raw_decode(value)
    except json.JSONDecodeError as exc:
        raise ValueError("model_response_not_json") from exc
    if value[end:].strip():
        raise ValueError("model_response_has_trailing_content")
    if not isinstance(parsed, dict):
        raise ValueError("model_action_not_object")
    if not isinstance(parsed.get("action"), str):
        raise ValueError("model_action_missing")
    return parsed


@dataclass(frozen=True)
class HttpRequestSpec:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float
    follow_redirects: bool = False


@dataclass(frozen=True)
class HttpResponseSpec:
    status: int
    body: bytes
    headers: Mapping[str, str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


class HttpTransport:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(jar),
            _NoRedirect(),
        )

    def __call__(self, spec: HttpRequestSpec) -> HttpResponseSpec:
        request = urllib.request.Request(
            spec.url,
            data=spec.body,
            method=spec.method,
            headers=spec.headers,
        )
        try:
            response = self._opener.open(request, timeout=spec.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(MAX_OBSERVATION_CHARS + 1)
            if len(body) > MAX_OBSERVATION_CHARS:
                body = body[:MAX_OBSERVATION_CHARS]
            return HttpResponseSpec(
                status=int(response.getcode()),
                body=body,
                headers=dict(response.headers.items()),
            )


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self.links: list[str] = []
        self._select_name: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        values = {str(name).lower(): value for name, value in attrs}
        for attribute in ("href", "src"):
            target = values.get(attribute)
            if tag in {"a", "form", "iframe", "link", "script"} and isinstance(
                target, str
            ):
                self.links.append(target)
        if tag == "form":
            current = {
                "method": str(values.get("method") or "GET").upper(),
                "action": values.get("action"),
                "fields": {},
            }
            self.forms.append(current)
            self._current = current
            return
        if self._current is None:
            return
        name = values.get("name")
        if tag == "select" and isinstance(name, str) and name:
            self._current["fields"].setdefault(name, set())
            self._select_name = name
            return
        if tag == "option" and self._select_name:
            value = values.get("value")
            if isinstance(value, str):
                allowed = self._current["fields"].get(self._select_name)
                if isinstance(allowed, set):
                    allowed.add(value)
            return
        if tag not in {"input", "button", "textarea"} or not isinstance(name, str) or not name:
            return
        field_type = str(values.get("type") or ("button" if tag == "button" else "text")).lower()
        if tag == "textarea" or field_type not in {
            "button", "checkbox", "hidden", "radio", "reset", "submit"
        }:
            self._current["fields"][name] = None
            return
        value = str(values.get("value") or "")
        current = self._current["fields"].get(name)
        if current is None and name in self._current["fields"]:
            return
        if not isinstance(current, set):
            current = set()
            self._current["fields"][name] = current
        current.add(value)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "select":
            self._select_name = None
        if tag == "form":
            self._current = None
            self._select_name = None


@dataclass(frozen=True)
class ActionResult:
    status: str
    observation: str
    solved: bool = False
    stop: bool = False
    candidate_recorded: bool = False
    candidate_sha256: str | None = None


class _RunLog:
    def __init__(self, scope: ChallengeScope, run_label: str) -> None:
        scope_id = scope.instance_binding_sha256()[:20]
        log_root = scope.work_root / ".arena"
        if log_root.exists() and log_root.is_symlink():
            raise ScopeDenied("unsafe_log_directory")
        log_root.mkdir(parents=True, exist_ok=True)
        directory = log_root / scope_id
        if directory.exists() and directory.is_symlink():
            raise ScopeDenied("unsafe_log_directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        self.evidence_path = directory / ".arena-evidence.jsonl"
        self.candidate_path = directory / ".arena-candidates.jsonl"
        self.run_label = run_label
        self.scope = scope

    @staticmethod
    def _append(path: Path, record: Mapping[str, Any]) -> bool:
        try:
            if path.exists() and path.is_symlink():
                return False
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return True
        except OSError:
            return False

    def evidence(
        self,
        *,
        step: int,
        action: str,
        status: str,
        observation: str,
        action_sha256: str,
        metrics: Mapping[str, int] | None = None,
    ) -> None:
        encoded = observation.encode("utf-8", errors="replace")
        record: dict[str, Any] = {
            "ts": int(time.time()),
            "run_label": self.run_label,
            "challenge": self.scope.name,
            "challenge_id": self.scope.workdir.name if self.scope.workdir else None,
            "instance_binding_sha256": self.scope.instance_binding_sha256(),
            "category": self.scope.category,
            "step": step,
            "action": action,
            "status": status,
            "action_sha256": action_sha256,
            "observation_bytes": len(encoded),
            "observation_sha256": hashlib.sha256(encoded).hexdigest(),
            "flag_candidate_sha256": [
                hashlib.sha256(value.encode("utf-8")).hexdigest()
                for value in FLAG_RE.findall(observation)
            ],
        }
        if metrics:
            record["metrics"] = {
                key: value for key, value in metrics.items()
                if key in {
                    "model_calls", "latency_ms", "prompt_tokens",
                    "completion_tokens", "total_tokens", "transport_attempts",
                }
                and isinstance(value, int)
                and 0 <= value <= 1_000_000_000
            }
        self._append(self.evidence_path, record)

    def candidate(
        self, *, step: int, kind: str, value: str, source_sha256: str
    ) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        persisted = self._append(self.candidate_path, {
            "ts": int(time.time()),
            "run_label": self.run_label,
            "challenge": self.scope.name,
            "challenge_id": self.scope.workdir.name if self.scope.workdir else None,
            "instance_binding_sha256": self.scope.instance_binding_sha256(),
            "category": self.scope.category,
            "step": step,
            "source_action_id": f"step-{step}",
            "source_body_sha256": source_sha256,
            "kind": kind,
            "candidate": value,
            "sha256": digest,
        })
        if not persisted:
            raise ScopeDenied("candidate_log_unavailable")
        return digest


class ActionExecutor:
    _PROGRAMS = {
        "base64", "checksec", "file", "grep", "head", "nm", "objdump",
        "readelf", "ROPgadget",
        "sha256sum", "size", "stat", "strings", "tail", "wc", "xxd",
    }
    _HEADER_ALLOWLIST = {
        "accept", "accept-language", "content-type", "origin", "referer",
        "user-agent", "x-requested-with",
    }
    _ACTION_FIELDS = {
        "command": {"action", "reason", "argv"},
        "read": {"action", "reason", "path", "offset", "length", "encoding"},
        "archive": {"action", "reason", "op", "path", "member"},
        "http": {
            "action", "reason", "intent", "method", "url", "headers", "body", "json"
        },
        "pwn": {"action", "reason", "op", "size", "timeout"},
        "tcp": {"action", "reason", "op", "size", "timeout", "text"},
        "submit": {"action", "reason", "flag"},
        "record": {"action", "reason", "kind", "value"},
        "finish": {"action", "reason"},
    }
    _FORBIDDEN_COMMAND_OPTIONS = {
        "checksec": {"--dir", "--proc", "--proc-all"},
        "file": {
            "-C", "--compile", "-f", "--files-from", "-m", "--magic-file"
        },
        "grep": {
            "-f", "--file", "-r", "-R", "--recursive", "--exclude-from"
        },
        "nm": {"--plugin"},
        "objdump": {"--plugin"},
        "sha256sum": {"-c", "--check"},
        "wc": {"--files0-from"},
    }

    def __init__(
        self,
        scope: ChallengeScope,
        run_bash: Callable[[str], str],
        submit_flag: Callable[[str], Mapping[str, Any]],
        *,
        http_transport: Callable[[HttpRequestSpec], HttpResponseSpec] | None = None,
        connect_pwn: Callable[[str, int, str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        submit_mode: str | None = None,
        run_label: str | None = None,
    ) -> None:
        self.scope = scope
        self._run_bash = run_bash
        self._submit_flag = submit_flag
        self._http = http_transport or HttpTransport()
        self._connect_pwn = connect_pwn
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_http_at: float | None = None
        self._http_requests = 0
        self._submissions: set[str] = set()
        self._observed_flags: set[str] = set()
        self._pwn_connections = 0
        self._pwn_receives = 0
        self._tcp_text_replies = 0
        self._tcp_text_reply_bytes = 0
        self._tcp_prompt_ready = False
        self._oracle_dialogue_profile = is_oracle_practice_scope(scope)
        self._socket_receive_tail = b""
        self._socket_state_epoch = 0
        self._socket = None
        self._step = 0
        mode = (submit_mode or os.environ.get("ARENA_SUBMIT_MODE") or "record").lower()
        if mode not in {"record", "submit"}:
            raise ScopeDenied("invalid_submit_mode")
        self.submit_mode = mode
        label = run_label or os.environ.get("ARENA_RUN_LABEL") or "unlabelled"
        self.run_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:80] or "unlabelled"
        scope_id = hashlib.sha256(scope.model_prompt.encode("utf-8")).hexdigest()[:16]
        self.scratch = Path(tempfile.gettempdir()).resolve() / "incypher-arena" / scope_id
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._artifacts = set(scope.files)
        self._artifact_depth = {path: 0 for path in scope.files}
        self._archive_expanded_bytes = 0
        self._run_log = _RunLog(scope, self.run_label)
        self._post_targets: dict[
            str, dict[str, set[str] | None] | None
        ] = {}
        self._allowed_http_paths: set[str] = set()
        if scope.entry_url:
            self._allowed_http_paths.add(_http_path(scope.entry_url))
        if scope.web_practice == "parcelport" and scope.origin:
            # Exact ID 42 documents a login form at this fixed route. The
            # response must still expose the POST fields before login is sent.
            self._allowed_http_paths.add("/login")
        self._graphql_mutations: dict[str, set[str]] = {}
        self._schema_drafts: dict[str, str] = {}
        self._parcel_recipients: dict[str, str] = {}
        if scope.web_practice == "schema_ghost" and scope.origin:
            graphql_url = scope.origin + "/graphql"
            self._allowed_http_paths.add("/graphql")
            self._post_targets[graphql_url] = None
        self._cumulative_observation = 0

    def set_step(self, step: int) -> None:
        self._step = max(0, int(step))

    def wait_for_web_readiness(self) -> None:
        """Pause once for an exact registered web instance to finish routing."""

        if self.scope.web_practice is None:
            raise ScopeDenied("web_readiness_wait_not_authorized")
        self._sleep(5.0)

    @staticmethod
    def _clean_environment_prefix() -> list[str]:
        return [
            "env", "-i",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME=/tmp", "TMPDIR=/tmp", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
            "timeout", "--signal=KILL", "25s",
        ]

    def _compile(self, argv: Sequence[str]) -> str:
        return shlex.join([*self._clean_environment_prefix(), *argv])

    def _cap(self, value: str) -> str:
        text = value if isinstance(value, str) else str(value or "")
        if len(text) > MAX_OBSERVATION_CHARS:
            omitted = len(text) - MAX_OBSERVATION_CHARS
            text = text[:32_768] + f"\n... <{omitted} chars omitted> ...\n" + text[-32_000:]
        remaining = MAX_CUMULATIVE_OBSERVATION_CHARS - self._cumulative_observation
        if remaining <= 0:
            return "<cumulative observation budget exhausted>"
        if len(text) > remaining:
            text = text[:remaining] + "\n<cumulative observation budget exhausted>"
        self._cumulative_observation += len(text)
        return text

    def _observe(self, value: str, candidate_source: str | None = None) -> str:
        text = self._cap(value)
        self._observed_flags.update(FLAG_RE.findall(
            text if candidate_source is None else candidate_source
        ))
        return text

    def _observation_result(
        self,
        value: str,
        *,
        candidate_source: str | None = None,
        source_bytes: bytes | None = None,
    ) -> ActionResult:
        text = self._observe(value, candidate_source)
        discovery_text = text if candidate_source is None else candidate_source
        candidates = list(dict.fromkeys(FLAG_RE.findall(discovery_text)))
        if not candidates:
            return ActionResult("ok", text)
        if FLAG_RE.search(text):
            redacted = FLAG_RE.sub("<flag-candidate-recorded>", text)
        else:
            redacted = "<flag-candidate-recorded>"
        if self.submit_mode == "record":
            source_body = (
                source_bytes if source_bytes is not None else text.encode("utf-8")
            )
            source_digest = hashlib.sha256(source_body).hexdigest()
            digests = [
                self._run_log.candidate(
                    step=self._step,
                    kind="flag",
                    value=candidate,
                    source_sha256=source_digest,
                )
                for candidate in candidates
            ]
            return ActionResult(
                "ok",
                redacted,
                stop=True,
                candidate_recorded=True,
                candidate_sha256=digests[0],
            )
        verdict_statuses = []
        for candidate in candidates:
            if candidate in self._submissions:
                continue
            if len(self._submissions) >= MAX_SUBMISSIONS:
                raise ScopeDenied("flag_submission_budget_exhausted")
            status = self._submit_candidate(candidate)
            self._submissions.add(candidate)
            verdict_statuses.append(status)
            if status in {"correct", "already_solved"}:
                return ActionResult(
                    "ok", redacted + "\nflag verdict: " + status, solved=True, stop=True
                )
        suffix = ("\nflag verdicts: " + ",".join(verdict_statuses)) if verdict_statuses else ""
        return ActionResult("ok", redacted + suffix)

    def _submit_candidate(self, flag: str) -> str:
        terminal = {"correct", "already_solved", "incorrect"}
        for attempt in range(2):
            try:
                verdict = self._submit_flag(flag) or {}
                status = str(verdict.get("status") or "error").strip().lower()
            except Exception:
                status = "error"
            if status in terminal:
                return status
            if attempt == 0:
                self._sleep(1.0)
        raise ScopeDenied("flag_submission_failed")

    def _path_allowed(self, value: str | Path, *, write: bool = False) -> Path:
        path = Path(value)
        try:
            resolved = path.resolve(strict=not write)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ScopeDenied("path_unavailable") from exc
        if _is_relative_to(resolved, self.scratch):
            return resolved
        if not write and resolved in self._artifacts:
            return resolved
        raise ScopeDenied("path_not_allowed")

    @staticmethod
    def _validate_token(token: str) -> None:
        if not isinstance(token, str) or not token or len(token) > 2048:
            raise ScopeDenied("invalid_argument")
        if "\x00" in token or "\n" in token or "\r" in token:
            raise ScopeDenied("argument_control_character")

    @staticmethod
    def _fixed_options(
        tokens: Sequence[str],
        flags: set[str],
        values: Mapping[str, re.Pattern[str]] | None = None,
    ) -> None:
        value_options = values or {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in flags:
                index += 1
                continue
            name, separator, inline = token.partition("=")
            if name not in value_options:
                raise ScopeDenied("command_option_forbidden")
            if separator:
                value = inline
                index += 1
            else:
                index += 1
                if index >= len(tokens):
                    raise ScopeDenied("command_option_value_missing")
                value = tokens[index]
                index += 1
            if value_options[name].fullmatch(value) is None:
                raise ScopeDenied("command_option_value_forbidden")

    def _validate_program_grammar(
        self, program: str, prefix: Sequence[str], artifact: str
    ) -> None:
        del artifact
        decimal = re.compile(r"[0-9]{1,9}")
        offset = re.compile(r"-?(?:[0-9]{1,12}|0x[0-9A-Fa-f]{1,16})")
        section = re.compile(r"[A-Za-z0-9_.-]{1,80}")
        if program == "grep":
            flags = {"-a", "--text", "-E", "--extended-regexp", "-F", "--fixed-strings",
                     "-G", "--basic-regexp", "-H", "--with-filename", "-h", "--no-filename",
                     "-i", "--ignore-case", "-n", "--line-number", "-o", "--only-matching"}
            values = {"-A": decimal, "--after-context": decimal, "-B": decimal,
                      "--before-context": decimal, "-C": decimal, "--context": decimal,
                      "-m": decimal, "--max-count": decimal}
            pattern_index = 0
            while pattern_index < len(prefix) and prefix[pattern_index].startswith("-"):
                token = prefix[pattern_index]
                name = token.split("=", 1)[0]
                if token in flags:
                    pattern_index += 1
                elif name in values:
                    if "=" in token:
                        if values[name].fullmatch(token.split("=", 1)[1]) is None:
                            raise ScopeDenied("command_option_value_forbidden")
                        pattern_index += 1
                    else:
                        if pattern_index + 1 >= len(prefix) or values[name].fullmatch(
                            prefix[pattern_index + 1]
                        ) is None:
                            raise ScopeDenied("command_option_value_forbidden")
                        pattern_index += 2
                else:
                    raise ScopeDenied("command_option_forbidden")
            patterns = list(prefix[pattern_index:])
            if len(patterns) != 1 or len(patterns[0]) > 500:
                raise ScopeDenied("command_pattern_forbidden")
            return

        grammars: dict[str, tuple[set[str], Mapping[str, re.Pattern[str]]]] = {
            "base64": ({"-d", "--decode", "-i", "--ignore-garbage"},
                       {"-w": decimal, "--wrap": decimal}),
            "file": ({"-b", "--brief", "-h", "--no-dereference", "-i", "--mime",
                      "--mime-encoding", "--mime-type", "-k", "--keep-going", "-L",
                      "--dereference", "-z", "--uncompress", "-Z", "--uncompress-noreport"}, {}),
            "head": ({"-q", "--quiet", "--silent", "-v", "--verbose"},
                     {"-c": decimal, "--bytes": decimal, "-n": decimal, "--lines": decimal}),
            "tail": ({"-q", "--quiet", "--silent", "-v", "--verbose"},
                     {"-c": decimal, "--bytes": decimal, "-n": decimal, "--lines": decimal}),
            "nm": ({"-a", "--debug-syms", "-A", "-o", "--print-file-name", "-C", "--demangle",
                    "-D", "--dynamic", "-g", "--extern-only", "-l", "--line-numbers", "-n",
                    "--numeric-sort", "-p", "--no-sort", "-r", "--reverse-sort", "-S",
                    "--print-size", "-s", "--print-armap", "-u", "--undefined-only"}, {}),
            "objdump": ({"-a", "--archive-headers", "-f", "--file-headers", "-p",
                         "--private-headers", "-h", "--section-headers", "-x", "--all-headers",
                         "-d", "--disassemble", "-D", "--disassemble-all", "-S", "--source",
                         "-s", "--full-contents", "-r", "--reloc", "-R", "--dynamic-reloc",
                         "-t", "--syms", "-T", "--dynamic-syms", "-C", "--demangle", "-w",
                         "--wide"}, {"-j": section, "--section": section,
                                    "-M": re.compile(r"(?:intel|att)")}),
            "readelf": ({"-a", "--all", "-h", "--file-header", "-l", "--program-headers",
                         "-S", "--section-headers", "-g", "--section-groups", "-t",
                         "--section-details", "-s", "--syms", "--dyn-syms", "-e", "--headers",
                         "-n", "--notes", "-r", "--relocs", "-u", "--unwind", "-d",
                         "--dynamic", "-V", "--version-info", "-A", "--arch-specific", "-I",
                         "--histogram", "-W", "--wide"},
                        {"-p": section, "--string-dump": section,
                         "-x": section, "--hex-dump": section}),
            "sha256sum": ({"-b", "--binary", "-t", "--text", "--tag"}, {}),
            "size": ({"-A", "--format=sysv", "-B", "--format=berkeley", "-G",
                      "--format=gnu", "-d", "-o", "-x", "-t", "--totals"}, {}),
            "stat": (set(), {}),
            "strings": ({"-a", "--all", "-d", "--data", "-f", "--print-file-name"},
                        {"-n": decimal, "--bytes": decimal,
                         "-t": re.compile(r"[dox]"), "--radix": re.compile(r"[dox]"),
                         "-e": re.compile(r"[sSblBL]"), "--encoding": re.compile(r"[sSblBL]")}),
            "wc": ({"-c", "--bytes", "-m", "--chars", "-l", "--lines", "-L",
                    "--max-line-length", "-w", "--words"}, {}),
            "xxd": ({"-a", "-b", "-e", "-i", "-p", "-ps", "-u"},
                    {"-c": decimal, "-cols": decimal, "-g": decimal, "-groupsize": decimal,
                     "-l": decimal, "-len": decimal, "-o": offset, "-s": offset,
                     "-seek": offset}),
        }
        if program == "checksec":
            flags = {"--extended", "--fortify", "--output=cli", "--output=csv",
                     "--output=json", "--verbose", "--file"}
            if any(token not in flags for token in prefix):
                raise ScopeDenied("command_option_forbidden")
            return
        if program == "ROPgadget":
            items = list(prefix)
            if items.count("--binary") != 1:
                raise ScopeDenied("command_option_forbidden")
            items.remove("--binary")
            allowed = {"--all", "--dump", "--multibr", "--noinstr", "--nojop", "--norop",
                       "--nosys", "--ropchain", "--silent"}
            if any(item not in allowed for item in items):
                raise ScopeDenied("command_option_forbidden")
            return
        flags, values = grammars[program]
        self._fixed_options(prefix, flags, values)

    def _validate_argv(self, argv: Any) -> list[str]:
        if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
            raise ScopeDenied("invalid_argv")
        if not all(isinstance(item, str) for item in argv):
            raise ScopeDenied("invalid_argv")
        for token in argv:
            self._validate_token(token)
            if FLAG_RE.search(token):
                raise ScopeDenied("command_contains_flag_candidate")
        program = argv[0]
        if program not in self._PROGRAMS:
            raise ScopeDenied("program_not_allowed")
        sealed_references: set[Path] = set()
        for token in argv[1:]:
            option_name = token.split("=", 1)[0]
            if option_name in self._FORBIDDEN_COMMAND_OPTIONS.get(program, set()):
                raise ScopeDenied("command_option_forbidden")
            lowered = token.lower()
            if any(marker in lowered for marker in (
                "follow-links", "debug-file-directory", "debug-dump=links"
            )):
                raise ScopeDenied("command_option_forbidden")
            candidate = Path(token)
            if candidate.is_absolute():
                allowed = self._path_allowed(candidate)
                if allowed in self._artifacts:
                    sealed_references.add(allowed)
            for fragment in re.findall(
                r"(?<![A-Za-z0-9._-])(?:/[A-Za-z0-9._+@%=-]+)+|[A-Za-z]:\\[^\s'\"]+",
                token,
            ):
                allowed = self._path_allowed(fragment)
                if allowed in self._artifacts:
                    sealed_references.add(allowed)
            if re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", token):
                raise ScopeDenied("relative_path_escape")
            if not candidate.is_absolute() and not token.startswith("-"):
                try:
                    if candidate.exists():
                        self._path_allowed(candidate)
                except OSError as exc:
                    raise ScopeDenied("path_unavailable") from exc
        if len(sealed_references) != 1:
            raise ScopeDenied("command_requires_sealed_artifact")
        sealed_text = str(next(iter(sealed_references)))
        final = argv[-1]
        if program == "checksec":
            approved_reference = final in {sealed_text, "--file=" + sealed_text}
            allowed_prefix = {
                "--extended", "--fortify", "--output=cli", "--output=csv",
                "--output=json", "--verbose", "--file",
            }
            if any(token not in allowed_prefix for token in argv[1:-1]):
                raise ScopeDenied("command_option_forbidden")
        elif program == "ROPgadget":
            approved_reference = final == sealed_text and "--binary" in argv[1:-1]
            if any(token.startswith("--binary=") for token in argv[1:-1]):
                raise ScopeDenied("command_option_forbidden")
        else:
            approved_reference = final == sealed_text
        if not approved_reference:
            raise ScopeDenied("sealed_artifact_must_be_final_argument")
        self._validate_program_grammar(program, argv[1:-1], sealed_text)
        return list(argv)

    def _run_command(self, action: Mapping[str, Any]) -> ActionResult:
        argv = self._validate_argv(action.get("argv"))
        output = self._run_bash(self._compile(argv))
        rendered = output or ""
        marker = re.match(r"^@@ARENA_EXIT:(\d{1,3})@@\n", rendered)
        if marker:
            rendered = rendered[marker.end():]
            if int(marker.group(1)) != 0:
                return ActionResult(
                    "error", FLAG_RE.sub("<untrusted-error-text>", self._cap(rendered))
                )
        return self._observation_result(rendered)

    def _run_read(self, action: Mapping[str, Any]) -> ActionResult:
        path = self._path_allowed(action.get("path"))
        offset = action.get("offset", 0)
        length = action.get("length", 4096)
        encoding = action.get("encoding", "text")
        if not isinstance(offset, int) or offset < 0:
            raise ScopeDenied("invalid_read_offset")
        if not isinstance(length, int) or not 1 <= length <= 65_536:
            raise ScopeDenied("invalid_read_length")
        if encoding not in {"text", "hex", "base64"}:
            raise ScopeDenied("invalid_read_encoding")
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        if encoding == "hex":
            rendered = data.hex()
        elif encoding == "base64":
            rendered = base64.b64encode(data).decode("ascii")
        else:
            rendered = data.decode("utf-8", errors="replace")
        return self._observation_result(rendered)

    @staticmethod
    def _safe_member_name(name: str) -> str:
        normalized = name.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            raise ScopeDenied("archive_member_path_escape")
        return "/".join(parts)

    @staticmethod
    def _zip_preflight(path: Path) -> None:
        size = path.stat().st_size
        with path.open("rb") as stream:
            tail_size = min(size, 65_557)
            stream.seek(size - tail_size)
            tail = stream.read(tail_size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ScopeDenied("zip_directory_invalid")
        try:
            (
                _signature, disk, central_disk, entries_disk, entries_total,
                central_size, central_offset, comment_size,
            ) = struct.unpack_from("<4s4H2LH", tail, offset)
        except struct.error as exc:
            raise ScopeDenied("zip_directory_invalid") from exc
        if (
            disk != 0
            or central_disk != 0
            or entries_disk != entries_total
            or entries_total > MAX_ARCHIVE_MEMBERS
            or entries_total == 0xFFFF
            or central_size > MAX_ZIP_CENTRAL_BYTES
            or central_offset + central_size > size
            or offset + 22 + comment_size != len(tail)
        ):
            raise ScopeDenied("zip_directory_budget_exhausted")

    def _archive_entries(self, path: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if zipfile.is_zipfile(path):
            self._zip_preflight(path)
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist()) > MAX_ARCHIVE_MEMBERS:
                    raise ScopeDenied("archive_member_budget_exhausted")
                for item in archive.infolist():
                    entries.append({
                        "name": self._safe_member_name(item.filename),
                        "size": item.file_size,
                        "kind": "dir" if item.is_dir() else "file",
                    })
            return entries
        if tarfile.is_tarfile(path):
            with tarfile.open(path, mode="r:*") as archive:
                for index, item in enumerate(archive):
                    if index >= MAX_ARCHIVE_MEMBERS:
                        raise ScopeDenied("archive_member_budget_exhausted")
                    entries.append({
                        "name": self._safe_member_name(item.name),
                        "size": item.size,
                        "kind": "file" if item.isfile() else "other",
                    })
            return entries
        if path.suffix.lower() in {".gz", ".gzip"}:
            return [{"name": path.stem, "size": None, "kind": "file"}]
        raise ScopeDenied("archive_format_unsupported")

    @staticmethod
    def _bounded_stream(stream, limit: int = 16 * 1024 * 1024) -> bytes:  # noqa: ANN001
        chunks = []
        total = 0
        while True:
            block = stream.read(min(65_536, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > limit:
                raise ScopeDenied("archive_member_too_large")
        return b"".join(chunks)

    def _extract_archive_member(self, path: Path, member: str | None) -> Path:
        depth = self._artifact_depth.get(path, 0)
        if depth >= MAX_ARCHIVE_DEPTH:
            raise ScopeDenied("archive_depth_exhausted")
        data: bytes
        safe_name: str
        if zipfile.is_zipfile(path):
            self._zip_preflight(path)
            if not isinstance(member, str):
                raise ScopeDenied("archive_member_required")
            safe_name = self._safe_member_name(member)
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist()) > MAX_ARCHIVE_MEMBERS:
                    raise ScopeDenied("archive_member_budget_exhausted")
                try:
                    item = archive.getinfo(member)
                except KeyError as exc:
                    raise ScopeDenied("archive_member_missing") from exc
                if item.is_dir() or item.file_size > 16 * 1024 * 1024:
                    raise ScopeDenied("archive_member_forbidden")
                with archive.open(item, "r") as stream:
                    data = self._bounded_stream(stream)
        elif tarfile.is_tarfile(path):
            if not isinstance(member, str):
                raise ScopeDenied("archive_member_required")
            safe_name = self._safe_member_name(member)
            with tarfile.open(path, mode="r:*") as archive:
                item = None
                for index, candidate in enumerate(archive):
                    if index >= MAX_ARCHIVE_MEMBERS:
                        raise ScopeDenied("archive_member_budget_exhausted")
                    if candidate.name == member:
                        item = candidate
                        break
                if item is None:
                    raise ScopeDenied("archive_member_missing")
                if not item.isfile() or item.size > 16 * 1024 * 1024:
                    raise ScopeDenied("archive_member_forbidden")
                stream = archive.extractfile(item)
                if stream is None:
                    raise ScopeDenied("archive_member_missing")
                with stream:
                    data = self._bounded_stream(stream)
        elif path.suffix.lower() in {".gz", ".gzip"}:
            safe_name = path.stem
            with gzip.open(path, "rb") as stream:
                data = self._bounded_stream(stream)
        else:
            raise ScopeDenied("archive_format_unsupported")
        if self._archive_expanded_bytes + len(data) > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ScopeDenied("archive_expansion_budget_exhausted")
        self._archive_expanded_bytes += len(data)
        digest = hashlib.sha256(data).hexdigest()
        destination = self.scratch / f"{digest[:16]}-{Path(safe_name).name}"
        destination.write_bytes(data)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        resolved = destination.resolve(strict=True)
        self._artifacts.add(resolved)
        self._artifact_depth[resolved] = depth + 1
        return resolved

    def _run_archive(self, action: Mapping[str, Any]) -> ActionResult:
        path = self._path_allowed(action.get("path"))
        operation = action.get("op")
        if operation == "list":
            entries = self._archive_entries(path)
            return self._observation_result(json.dumps(
                entries, sort_keys=True, separators=(",", ":")
            ))
        if operation == "extract":
            extracted = self._extract_archive_member(path, action.get("member"))
            observation = json.dumps({
                "path": str(extracted),
                "size": extracted.stat().st_size,
                "sha256": hashlib.sha256(extracted.read_bytes()).hexdigest(),
            }, sort_keys=True, separators=(",", ":"))
            return self._observation_result(observation)
        raise ScopeDenied("invalid_archive_operation")


    def _run_http(self, action: Mapping[str, Any]) -> ActionResult:
        if self._http_requests >= MAX_HTTP_REQUESTS:
            raise ScopeDenied("http_budget_exhausted")
        method = str(action.get("method") or "GET").upper()
        if method not in {"GET", "HEAD", "POST"}:
            raise ScopeDenied("http_method_forbidden")
        intent = action.get("intent")
        if method == "POST":
            if intent not in {"harmless_probe", "workflow_transition"}:
                raise ScopeDenied("http_intent_forbidden")
        elif intent not in {"baseline", "harmless_probe"}:
            raise ScopeDenied("http_intent_forbidden")
        url = self.scope.require_url(action.get("url"))
        parsed_url = urllib.parse.urlsplit(url)
        if _destructive_text(parsed_url.path + "?" + parsed_url.query):
            raise ScopeDenied("destructive_http_route_forbidden")
        if _http_path(url) not in self._allowed_http_paths:
            raise ScopeDenied("http_path_not_observed")
        if (
            self.scope.web_practice == "parcelport"
            and parsed_url.path.startswith("/parcel/")
        ):
            expected_recipient = self._parcel_recipients.get(parsed_url.path)
            if expected_recipient is None or urllib.parse.parse_qsl(
                parsed_url.query, keep_blank_values=True
            ) != [("recipient", expected_recipient)]:
                raise ScopeDenied("parcel_authorization_probe_not_observed")
        if method == "POST" and url not in self._post_targets:
            raise ScopeDenied("http_transition_not_observed")
        raw_headers = action.get("headers") or {}
        if not isinstance(raw_headers, dict) or len(raw_headers) > 16:
            raise ScopeDenied("invalid_http_headers")
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) incypher-arena/1.0"}
        for name, value in raw_headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ScopeDenied("invalid_http_headers")
            if name.lower() not in self._HEADER_ALLOWLIST or len(value) > 2048:
                raise ScopeDenied("http_header_forbidden")
            if _destructive_text(value):
                raise ScopeDenied("destructive_http_header_forbidden")
            headers[name] = value
        body_value = action.get("body")
        json_value = action.get("json")
        if body_value is not None and json_value is not None:
            raise ScopeDenied("ambiguous_http_body")
        if method != "POST" and (body_value is not None or json_value is not None):
            raise ScopeDenied("http_body_forbidden")
        body = None
        if json_value is not None:
            if not isinstance(json_value, dict):
                raise ScopeDenied("invalid_http_json")
            body = json.dumps(json_value, separators=(",", ":")).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif body_value is not None:
            if not isinstance(body_value, str):
                raise ScopeDenied("invalid_http_body")
            body = body_value.encode("utf-8")
        if body is not None and len(body) > 32_768:
            raise ScopeDenied("http_body_too_large")
        if method == "POST":
            allowed_fields = self._post_targets[url]
            if allowed_fields is None:
                if json_value is None or set(json_value) - {
                    "query", "variables", "operationName", "extensions"
                }:
                    raise ScopeDenied("graphql_request_invalid")
                if _destructive_text(json.dumps(
                    json_value, sort_keys=True, separators=(",", ":")
                )):
                    raise ScopeDenied("destructive_http_body_forbidden")
                operation, root_fields = self._graphql_operation(json_value.get("query"))
                if intent == "harmless_probe" and operation != "query":
                    raise ScopeDenied("graphql_mutation_not_authorized")
                if intent == "workflow_transition":
                    if self.scope.web_practice == "schema_ghost":
                        transition_allowed = self._schema_ghost_transition_allowed(
                            json_value.get("query"), url
                        )
                    else:
                        transition_allowed = set(root_fields).issubset(
                            self._graphql_mutations.get(url, set())
                        )
                    if operation != "mutation" or not transition_allowed:
                        raise ScopeDenied("graphql_mutation_not_observed")
            else:
                if intent != "workflow_transition":
                    raise ScopeDenied("http_intent_forbidden")
                if json_value is not None:
                    submitted = {key: [str(value)] for key, value in json_value.items()}
                elif body_value is not None:
                    submitted = urllib.parse.parse_qs(
                        body_value, keep_blank_values=True, strict_parsing=False
                    )
                else:
                    submitted = {}
                if set(submitted) - set(allowed_fields):
                    raise ScopeDenied("http_transition_field_not_observed")
                for field, values in submitted.items():
                    permitted = allowed_fields[field]
                    if permitted is not None and any(value not in permitted for value in values):
                        raise ScopeDenied("http_transition_value_not_observed")
                rendered_body = body_value if body_value is not None else json.dumps(
                    json_value, sort_keys=True, separators=(",", ":")
                )
                if _destructive_text(rendered_body):
                    raise ScopeDenied("destructive_http_body_forbidden")
        request = HttpRequestSpec(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=20.0,
            follow_redirects=False,
        )
        response = self._dispatch_http(request)
        lines = [f"HTTP {response.status}"]
        for name in ("Content-Type", "Location"):
            value = next(
                (item for key, item in response.headers.items() if key.lower() == name.lower()),
                None,
            )
            if value is None:
                continue
            if name == "Location":
                try:
                    resolved = urllib.parse.urljoin(url, value)
                    resolved = self.scope.require_url(resolved)
                    if not _destructive_text(urllib.parse.urlsplit(resolved).path):
                        self._allowed_http_paths.add(_http_path(resolved))
                    value = resolved
                except ScopeDenied:
                    value = "<off-origin-redacted>"
            lines.append(f"{name}: {value[:2048]}")
        if method != "HEAD":
            lines.extend(["", response.body.decode("utf-8", errors="replace")])
            content_type = next((
                value for key, value in response.headers.items()
                if key.lower() == "content-type"
            ), "")
            self._discover_http_transitions(
                url, response.body, response.status, content_type, request.body
            )
        return self._observation_result("\n".join(lines))

    def _schema_ghost_transition_allowed(self, query: Any, url: str) -> bool:
        if self.scope.origin is None or url != self.scope.origin + "/graphql":
            return False
        if not isinstance(query, str):
            return False
        normalized = re.sub(r"[\s,]+", "", query)
        if normalized in {"mutation{createDraf}", "mutation{promoteDraf}"}:
            return True
        if normalized == "mutation{createDraft{idtitlestateticket}}":
            return "createDraft" in self._graphql_mutations.get(url, set())
        match = re.fullmatch(
            r'mutation\{promoteDraft\(id:"([A-Za-z0-9._~-]{1,128})"'
            r'ticket:"([A-Za-z0-9._~-]{1,256})"\)\{idstateticket\}\}',
            normalized,
        )
        return bool(
            match
            and "promoteDraft" in self._graphql_mutations.get(url, set())
            and self._schema_drafts.get(match.group(1)) == match.group(2)
        )

    def _dispatch_http(self, request: HttpRequestSpec) -> HttpResponseSpec:
        for attempt in range(2):
            if self._http_requests >= MAX_HTTP_REQUESTS:
                raise ScopeDenied("http_budget_exhausted")
            now = self._monotonic()
            if self._last_http_at is not None:
                delay = 1.0 - (now - self._last_http_at)
                if delay > 0:
                    self._sleep(delay)
            self._http_requests += 1
            self._last_http_at = self._monotonic()
            try:
                response = self._http(request)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 0:
                    self._sleep(1.0)
                    continue
                raise ScopeDenied("http_request_failed") from exc
            if response.status not in {408, 429} and not 500 <= response.status <= 599:
                return response
            if attempt == 0:
                retry_after = next((
                    value for key, value in response.headers.items()
                    if key.lower() == "retry-after"
                ), "1")
                try:
                    delay = max(0.0, min(5.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = 1.0
                self._sleep(delay)
                continue
            return response
        raise ScopeDenied("http_request_failed")

    @staticmethod
    def _graphql_operation(value: Any) -> tuple[str, tuple[str, ...]]:
        if not isinstance(value, str) or not value or len(value) > 20_000:
            raise ScopeDenied("graphql_request_invalid")
        token_re = re.compile(
            r'''\s+|#[^\r\n]*|"""(?:.|\r|\n)*?"""|"(?:\\.|[^"\\])*"|'''
            r'''\.\.\.|[_A-Za-z][_0-9A-Za-z]*|-?(?:[0-9]+(?:\.[0-9]+)?)|'''
            r'''[$!():=@\[\]{|}&,]'''
        )
        tokens: list[str] = []
        position = 0
        while position < len(value):
            match = token_re.match(value, position)
            if not match:
                raise ScopeDenied("graphql_request_invalid")
            token = match.group(0)
            position = match.end()
            if not token.isspace() and not token.startswith("#"):
                tokens.append(token)
        if not tokens:
            raise ScopeDenied("graphql_request_invalid")

        def skip_balanced(index: int) -> int:
            pairs = {"(": ")", "[": "]", "{": "}"}
            stack = [pairs[tokens[index]]]
            index += 1
            while index < len(tokens) and stack:
                token = tokens[index]
                if token in pairs:
                    stack.append(pairs[token])
                elif token == stack[-1]:
                    stack.pop()
                index += 1
            if stack:
                raise ScopeDenied("graphql_request_invalid")
            return index

        index = 0
        operation = "query"
        if tokens[index] in {"query", "mutation"}:
            operation = tokens[index]
            index += 1
            if index < len(tokens) and re.fullmatch(
                r"[_A-Za-z][_0-9A-Za-z]*", tokens[index]
            ):
                index += 1
            if index < len(tokens) and tokens[index] == "(":
                index = skip_balanced(index)
            while index < len(tokens) and tokens[index] == "@":
                index += 1
                if index >= len(tokens) or re.fullmatch(
                    r"[_A-Za-z][_0-9A-Za-z]*", tokens[index]
                ) is None:
                    raise ScopeDenied("graphql_request_invalid")
                index += 1
                if index < len(tokens) and tokens[index] == "(":
                    index = skip_balanced(index)
        elif tokens[index] != "{":
            raise ScopeDenied("graphql_request_invalid")
        if index >= len(tokens) or tokens[index] != "{":
            raise ScopeDenied("graphql_request_invalid")
        index += 1
        fields: list[str] = []
        while index < len(tokens) and tokens[index] != "}":
            if tokens[index] == ",":
                index += 1
                continue
            if tokens[index] == "..." or re.fullmatch(
                r"[_A-Za-z][_0-9A-Za-z]*", tokens[index]
            ) is None:
                raise ScopeDenied("graphql_request_invalid")
            field = tokens[index]
            index += 1
            if index < len(tokens) and tokens[index] == ":":
                index += 1
                if index >= len(tokens) or re.fullmatch(
                    r"[_A-Za-z][_0-9A-Za-z]*", tokens[index]
                ) is None:
                    raise ScopeDenied("graphql_request_invalid")
                field = tokens[index]
                index += 1
            fields.append(field)
            if index < len(tokens) and tokens[index] == "(":
                index = skip_balanced(index)
            while index < len(tokens) and tokens[index] == "@":
                index += 1
                if index >= len(tokens) or re.fullmatch(
                    r"[_A-Za-z][_0-9A-Za-z]*", tokens[index]
                ) is None:
                    raise ScopeDenied("graphql_request_invalid")
                index += 1
                if index < len(tokens) and tokens[index] == "(":
                    index = skip_balanced(index)
            if index < len(tokens) and tokens[index] == "{":
                index = skip_balanced(index)
        if index >= len(tokens) or tokens[index] != "}" or not fields:
            raise ScopeDenied("graphql_request_invalid")
        index += 1
        if index != len(tokens):
            raise ScopeDenied("graphql_request_invalid")
        return operation, tuple(fields)

    def _allow_parcel_tracking_window(self, number: int) -> None:
        for delta in range(-4, 5):
            candidate = number + delta
            if 0 <= candidate <= 999_999:
                self._allowed_http_paths.add(f"/track/PP-{candidate:06d}")

    def _discover_http_transitions(
        self,
        request_url: str,
        body: bytes,
        status: int,
        content_type: str,
        request_body: bytes | None = None,
    ) -> None:
        if not 200 <= status < 400:
            return
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            try:
                response_value = json.loads(body.decode("utf-8"))
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                response_value = None
            if self.scope.web_practice == "schema_ghost" and isinstance(
                response_value, dict
            ):
                self._discover_schema_ghost_transition(
                    request_url, request_body, response_value
                )
            if self.scope.web_practice == "parcelport" and isinstance(
                response_value, dict
            ):
                self._discover_parcel_transition(request_url, response_value)
        if media_type == "application/json" and self._post_targets.get(request_url) is None:
            try:
                fields = response_value["data"]["__schema"]["mutationType"]["fields"]
                names = {
                    item["name"] for item in fields
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", item["name"])
                    and not _destructive_text(item["name"])
                }
                self._graphql_mutations.setdefault(request_url, set()).update(names)
            except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                pass
            return
        if media_type not in {"text/html", "application/xhtml+xml"}:
            return
        text = body.decode("utf-8", errors="replace")
        parser = _FormParser()
        try:
            parser.feed(text[:MAX_OBSERVATION_CHARS])
        except Exception:
            return
        for link in parser.links:
            target = urllib.parse.urljoin(request_url, link)
            try:
                target = self.scope.require_url(target)
            except ScopeDenied:
                continue
            if not _destructive_text(urllib.parse.urlsplit(target).path):
                target_path = _http_path(target)
                self._allowed_http_paths.add(target_path)
                if self.scope.web_practice == "parcelport":
                    match = re.fullmatch(r"(/track/PP-)([0-9]{6})", target_path)
                    if match:
                        self._allow_parcel_tracking_window(int(match.group(2)))
        if self.scope.web_practice == "parcelport":
            for match in re.finditer(r"\bPP-([0-9]{6})\b", text):
                self._allow_parcel_tracking_window(int(match.group(1)))
        for form in parser.forms:
            target = urllib.parse.urljoin(request_url, form["action"] or request_url)
            try:
                target = self.scope.require_url(target)
            except ScopeDenied:
                continue
            if _destructive_text(urllib.parse.urlsplit(target).path):
                continue
            self._allowed_http_paths.add(_http_path(target))
            if form["method"] != "POST":
                continue
            fields = dict(form["fields"])
            current = self._post_targets.get(target)
            if isinstance(current, dict):
                for name, permitted in current.items():
                    if name not in fields:
                        fields[name] = permitted
                    elif fields[name] is not None and permitted is not None:
                        fields[name].update(permitted)
                    else:
                        fields[name] = None
            self._post_targets[target] = fields
        for match in re.finditer(r"(?i)[\"'](/[^\"']*graphql[^\"']*)[\"']", text):
            target = urllib.parse.urljoin(request_url, match.group(1))
            try:
                target = self.scope.require_url(target)
            except ScopeDenied:
                continue
            self._allowed_http_paths.add(_http_path(target))
            self._post_targets[target] = None

    def _discover_schema_ghost_transition(
        self,
        request_url: str,
        request_body: bytes | None,
        response: Mapping[str, Any],
    ) -> None:
        if self.scope.origin is None or request_url != self.scope.origin + "/graphql":
            return
        try:
            request_value = json.loads((request_body or b"").decode("utf-8"))
            query = request_value["query"]
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(query, str):
            return
        normalized = re.sub(r"[\s,]+", "", query)
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        if normalized == "mutation{createDraf}" and "createDraft" in response_text:
            self._graphql_mutations.setdefault(request_url, set()).add("createDraft")
        if normalized == "mutation{promoteDraf}" and "promoteDraft" in response_text:
            self._graphql_mutations.setdefault(request_url, set()).add("promoteDraft")
        if normalized != "mutation{createDraft{idtitlestateticket}}":
            return
        try:
            created = response["data"]["createDraft"]
            draft_id = created["id"]
            ticket = created["ticket"]
        except (KeyError, TypeError):
            return
        if (
            isinstance(draft_id, str)
            and re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", draft_id)
            and isinstance(ticket, str)
            and re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", ticket)
        ):
            self._schema_drafts[draft_id] = ticket

    def _discover_parcel_transition(
        self, request_url: str, response: Mapping[str, Any]
    ) -> None:
        path = _http_path(request_url)
        if re.fullmatch(r"/track/PP-[0-9]{6}", path) is None:
            return
        if response.get("restricted") is not True:
            return
        reference = response.get("reference")
        recipient = response.get("recipient_id")
        if not isinstance(reference, str) or re.fullmatch(
            r"[A-Za-z0-9._~-]{1,256}", reference
        ) is None:
            return
        if not isinstance(recipient, (str, int)) or isinstance(recipient, bool):
            return
        recipient_text = str(recipient)
        if re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", recipient_text) is None:
            return
        parcel_path = "/parcel/" + reference
        self._allowed_http_paths.add(parcel_path)
        self._parcel_recipients[parcel_path] = recipient_text

    def _connector(self):
        if self._connect_pwn is not None:
            return self._connect_pwn
        try:
            from ctfd import connect_pwn  # type: ignore
        except Exception as exc:
            raise ScopeDenied("official_pwn_connector_unavailable") from exc
        return connect_pwn

    @staticmethod
    def _render_bytes(value: bytes) -> str:
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return "hex:" + value.hex()
        if all(character in "\n\r\t" or character.isprintable() for character in text):
            return text
        return "hex:" + value.hex()

    def _socket_observation_result(self, value: bytes) -> ActionResult:
        combined = self._socket_receive_tail + value
        candidate_source = combined.decode("utf-8", errors="ignore")
        rendered = self._render_bytes(value)
        if FLAG_RE.search(candidate_source):
            self._socket_receive_tail = b""
            return self._observation_result(
                rendered,
                candidate_source=candidate_source,
                source_bytes=combined,
            )
        self._socket_receive_tail = combined[-256:]
        return self._observation_result(rendered)

    def _run_pwn(self, action: Mapping[str, Any]) -> ActionResult:
        if not self.scope.pwn_host or not self.scope.pwn_port or not self.scope.team_key:
            raise ScopeDenied("pwn_not_in_scope")
        operation = action.get("op")
        if operation == "connect":
            if self._socket is not None:
                raise ScopeDenied("pwn_already_connected")
            if self._pwn_connections >= MAX_PWN_CONNECTIONS:
                raise ScopeDenied("pwn_connection_budget_exhausted")
            self._pwn_connections += 1
            self._socket_state_epoch += 1
            connector = self._connector()
            for attempt in range(MAX_PWN_CONNECT_ATTEMPTS):
                try:
                    self._socket = connector(
                        self.scope.pwn_host,
                        self.scope.pwn_port,
                        self.scope.team_key,
                    )
                    break
                except Exception as exc:
                    if attempt + 1 >= MAX_PWN_CONNECT_ATTEMPTS:
                        raise ScopeDenied("pwn_connect_failed") from exc
                    self._sleep(2.0)
            self._socket_receive_tail = b""
            return ActionResult("ok", "official PoW connector established the scoped socket")
        if self._socket is None:
            raise ScopeDenied("pwn_not_connected")
        if operation == "recv":
            receive_limit = (
                MAX_ORACLE_PWN_RECEIVES
                if self._oracle_dialogue_profile
                else MAX_PWN_RECEIVES
            )
            if self._pwn_receives >= receive_limit:
                raise ScopeDenied("pwn_receive_budget_exhausted")
            size = action.get("size", 4096)
            timeout = action.get("timeout", 3.0)
            if not isinstance(size, int) or not 1 <= size <= MAX_OBSERVATION_CHARS:
                raise ScopeDenied("invalid_recv_size")
            if not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 10.0:
                raise ScopeDenied("invalid_recv_timeout")
            try:
                self._pwn_receives += 1
                self._socket_state_epoch += 1
                self._socket.settimeout(float(timeout))
                data = self._socket.recv(size)
            except Exception as exc:
                raise ScopeDenied("pwn_receive_failed") from exc
            if data:
                self._tcp_prompt_ready = True
            return self._socket_observation_result(data)
        if operation == "send":
            raise ScopeDenied("pwn_send_requires_proof_permit")
        if operation == "close":
            self.close()
            return ActionResult("ok", "scoped socket closed")
        raise ScopeDenied("invalid_pwn_operation")

    def _run_tcp(self, action: Mapping[str, Any]) -> ActionResult:
        if not self.scope.allow_tcp_dialogue:
            raise ScopeDenied("tcp_dialogue_not_allowed")
        operation = action.get("op")
        if operation in {"connect", "recv", "close"}:
            if "text" in action:
                raise ScopeDenied("tcp_text_unexpected")
            return self._run_pwn(action)
        if operation == "sendline":
            if "size" in action or "timeout" in action:
                raise ScopeDenied("tcp_sendline_fields_invalid")
            if self._socket is None:
                raise ScopeDenied("pwn_not_connected")
            if not self._tcp_prompt_ready:
                raise ScopeDenied("tcp_reply_requires_prompt")
            text = action.get("text")
            if (
                not isinstance(text, str)
                or not text
                or len(text) > MAX_TCP_TEXT_REPLY_BYTES
                or "\r" in text
                or "\n" in text
                or any(
                    ord(character) < 0x20 or ord(character) > 0x7E
                    for character in text
                )
            ):
                raise ScopeDenied("tcp_reply_invalid")
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_TCP_TEXT_REPLY_BYTES:
                raise ScopeDenied("tcp_reply_invalid")
            if (
                FLAG_RE.search(text)
                or TOKEN_RE.search(text)
                or ASSIGNMENT_SECRET_RE.search(text)
                or (self.scope.team_key and self.scope.team_key in text)
            ):
                raise ScopeDenied("tcp_reply_secret_or_flag")
            reply_limit = (
                MAX_ORACLE_TCP_TEXT_REPLIES
                if self._oracle_dialogue_profile
                else MAX_TCP_TEXT_REPLIES
            )
            if self._tcp_text_replies >= reply_limit:
                raise ScopeDenied("tcp_reply_count_exhausted")
            if (
                self._tcp_text_reply_bytes + len(encoded) + 1
                > MAX_TCP_TEXT_TOTAL_BYTES
            ):
                raise ScopeDenied("tcp_reply_bytes_exhausted")
            self._tcp_text_replies += 1
            self._tcp_text_reply_bytes += len(encoded) + 1
            self._tcp_prompt_ready = False
            self._socket_receive_tail = b""
            self._socket_state_epoch += 1
            try:
                self._socket.sendall(encoded + b"\n")
            except Exception as exc:
                raise ScopeDenied("tcp_reply_failed") from exc
            return ActionResult("ok", "sent one bounded ASCII protocol line")
        raise ScopeDenied("invalid_tcp_operation")

    def _run_submit(self, action: Mapping[str, Any]) -> ActionResult:
        flag = action.get("flag")
        if not isinstance(flag, str) or FLAG_RE.fullmatch(flag) is None:
            raise ScopeDenied("invalid_flag_format")
        if flag not in self._observed_flags:
            raise ScopeDenied("flag_not_observed")
        if flag in self._submissions:
            raise ScopeDenied("duplicate_flag_submission")
        if len(self._submissions) >= MAX_SUBMISSIONS:
            raise ScopeDenied("flag_submission_budget_exhausted")
        if self.submit_mode == "record":
            self._submissions.add(flag)
            digest = self._run_log.candidate(
                step=self._step,
                kind="flag",
                value=flag,
                source_sha256=hashlib.sha256(flag.encode("utf-8")).hexdigest(),
            )
            return ActionResult(
                "ok",
                "candidate recorded for offline comparison: sha256=" + digest,
                stop=True,
                candidate_recorded=True,
                candidate_sha256=digest,
            )
        status = self._submit_candidate(flag)
        self._submissions.add(flag)
        solved = status in {"correct", "already_solved"}
        return ActionResult(
            "ok", f"flag verdict: {status}", solved=solved, stop=solved
        )

    def _run_record(self, action: Mapping[str, Any]) -> ActionResult:
        kind = action.get("kind")
        value = action.get("value")
        if kind not in {"hypothesis", "pwn_payload"}:
            raise ScopeDenied("record_kind_forbidden")
        if not isinstance(value, str) or not value or len(value) > 65_536:
            raise ScopeDenied("record_value_invalid")
        if TOKEN_RE.search(value) or ASSIGNMENT_SECRET_RE.search(value):
            raise ScopeDenied("record_value_secret_shaped")
        digest = self._run_log.candidate(
            step=self._step,
            kind=kind,
            value=value,
            source_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
        )
        return ActionResult(
            "ok", f"{kind} recorded for offline comparison: sha256={digest}"
        )

    def _validate_action_schema(self, action: Mapping[str, Any]) -> str:
        kind = action.get("action")
        if kind not in self._ACTION_FIELDS:
            raise ScopeDenied("unknown_action")
        unknown = set(action) - self._ACTION_FIELDS[kind]
        if unknown:
            raise ScopeDenied("unknown_action_field")
        reason = action.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ScopeDenied("invalid_action_reason")
        if kind != "submit":
            stack = [value for key, value in action.items() if key != "reason"]
            while stack:
                value = stack.pop()
                if isinstance(value, str):
                    decoded = _fully_unquote(value)
                    if FLAG_RE.search(value) or FLAG_RE.search(decoded):
                        raise ScopeDenied("action_contains_flag_candidate")
                elif isinstance(value, Mapping):
                    stack.extend(value.keys())
                    stack.extend(value.values())
                elif isinstance(value, (list, tuple)):
                    stack.extend(value)
        return str(kind)

    @staticmethod
    def semantic_action(action: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in action.items() if key != "reason"}

    def repetition_context(self, action: Mapping[str, Any]) -> int | None:
        """Bind socket-action deduplication to the current connection state."""
        if action.get("action") in {"pwn", "tcp"}:
            return self._socket_state_epoch
        return None

    def record_denial(self, action: Mapping[str, Any], reason: str) -> None:
        kind = str(action.get("action") or "invalid")
        semantic = self.semantic_action(action)
        digest = hashlib.sha256(json.dumps(
            semantic, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")).hexdigest()
        self._run_log.evidence(
            step=self._step,
            action=kind,
            status="denied",
            observation=reason,
            action_sha256=digest,
        )

    def record_event(self, action: str, status: str, observation: str) -> None:
        digest = hashlib.sha256(
            (action + "\0" + status + "\0" + observation).encode("utf-8")
        ).hexdigest()
        self._run_log.evidence(
            step=self._step,
            action=action,
            status=status,
            observation=observation,
            action_sha256=digest,
        )

    def record_deterministic_candidate(
        self,
        *,
        flag: str,
        analyzer: str,
        artifact_sha256: str,
    ) -> ActionResult:
        """Route a hash-gated analyzer result through normal record/submit policy."""

        if (
            not isinstance(analyzer, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", analyzer) is None
        ):
            raise ScopeDenied("deterministic_analyzer_invalid")
        if (
            not isinstance(artifact_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
        ):
            raise ScopeDenied("deterministic_digest_invalid")
        if not isinstance(flag, str) or FLAG_RE.fullmatch(flag) is None:
            raise ScopeDenied("deterministic_candidate_invalid")
        candidate_digest = hashlib.sha256(flag.encode("utf-8")).hexdigest()
        result = self._observation_result(flag)
        safe_observation = (
            f"analyzer={analyzer};artifact_sha256={artifact_sha256};"
            f"candidate_sha256={candidate_digest};candidate_processed"
        )
        action_digest = hashlib.sha256(
            (analyzer + "\0" + artifact_sha256).encode("utf-8")
        ).hexdigest()
        self._run_log.evidence(
            step=self._step,
            action="deterministic",
            status=result.status,
            observation=safe_observation,
            action_sha256=action_digest,
        )
        return result

    def record_model_metrics(
        self,
        metrics: Mapping[str, Any] | None,
        *,
        error_code: str | None = None,
    ) -> None:
        safe_metrics = {"model_calls": 1}
        if isinstance(metrics, Mapping):
            for key in (
                "latency_ms", "prompt_tokens", "completion_tokens", "total_tokens",
                "transport_attempts",
            ):
                value = metrics.get(key)
                if isinstance(value, int) and 0 <= value <= 1_000_000_000:
                    safe_metrics[key] = value
        digest = hashlib.sha256(json.dumps(
            safe_metrics, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        safe_error = error_code if error_code and re.fullmatch(
            r"llm_[a-z0-9_]{1,80}", error_code
        ) else None
        self._run_log.evidence(
            step=self._step,
            action="model",
            status="error" if safe_error else "ok",
            observation=safe_error or "model_call",
            action_sha256=digest,
            metrics=safe_metrics,
        )

    def execute(self, action: Mapping[str, Any]) -> ActionResult:
        if not isinstance(action, Mapping):
            raise ScopeDenied("action_not_object")
        kind = self._validate_action_schema(action)
        handlers = {
            "command": self._run_command,
            "read": self._run_read,
            "archive": self._run_archive,
            "http": self._run_http,
            "pwn": self._run_pwn,
            "tcp": self._run_tcp,
            "submit": self._run_submit,
            "record": self._run_record,
        }
        if kind == "finish":
            result = ActionResult("ok", "agent stopped without a verified flag", stop=True)
        elif kind in handlers:
            result = handlers[kind](action)
        else:
            raise ScopeDenied("unknown_action")
        digest = hashlib.sha256(json.dumps(
            self.semantic_action(action),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        self._run_log.evidence(
            step=self._step,
            action=str(kind),
            status=result.status,
            observation=result.observation,
            action_sha256=digest,
        )
        return result

    def inventory(self) -> str:
        rows = []
        remaining_hash_bytes = MAX_INVENTORY_HASH_BYTES
        for path in self.scope.files:
            declared_size = path.stat().st_size
            if declared_size > remaining_hash_bytes:
                with path.open("rb") as stream:
                    first = stream.read(65_536)
                    tail = b""
                    if declared_size > len(first):
                        stream.seek(max(0, declared_size - 65_536))
                        tail = stream.read(65_536)
                sample = first + tail
                rows.append({
                    "path": str(path),
                    "size": declared_size,
                    "sha256": None,
                    "sample_sha256": hashlib.sha256(sample).hexdigest(),
                    "inventory_truncated": True,
                    "first16_hex": first[:16].hex(),
                })
                continue
            digest = hashlib.sha256()
            size = 0
            first = b""
            with path.open("rb") as stream:
                while True:
                    block = stream.read(65_536)
                    if not block:
                        break
                    if not first:
                        first = block[:16]
                    digest.update(block)
                    size += len(block)
            remaining_hash_bytes -= size
            rows.append({
                "path": str(path), "size": size, "sha256": digest.hexdigest(),
                "sample_sha256": None,
                "inventory_truncated": False,
                "first16_hex": first.hex(),
            })
        return json.dumps(rows, sort_keys=True, separators=(",", ":"))

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self._tcp_prompt_ready = False
        self._socket_receive_tail = b""
        if sock is not None:
            self._socket_state_epoch += 1
            try:
                sock.close()
            except Exception:
                pass


@dataclass(frozen=True)
class ModelConfiguration:
    api_key: str = field(repr=False)
    endpoint: str
    model: str
    max_tokens: int = 1536
    timeout: float = 45.0
    retries: int = 1
    token_parameter: str = "max_tokens"
    referer: str = "https://in-cypher.com"
    title: str = "INCYPHER Arena Agent"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        env = environment if environment is not None else os.environ
        key = (env.get("LLM_API_KEY") or "").strip()
        base = (env.get("LLM_BASE_URL") or "").strip().rstrip("/")
        model = (env.get("LLM_MODEL") or "").strip()
        if not key or not base or not model:
            raise ValueError("llm_configuration_missing")
        if base.endswith("/chat/completions"):
            endpoint = base
        else:
            endpoint = base + "/chat/completions"
        parsed = urllib.parse.urlsplit(endpoint)
        allow_http = env.get("LLM_ALLOW_HTTP") == "1"
        if parsed.scheme == "http" and parsed.hostname:
            hostname = parsed.hostname.rstrip(".").lower()
            try:
                private_literal = ipaddress.ip_address(hostname).is_private
            except ValueError:
                private_literal = False
            allow_http = allow_http or private_literal or hostname == "localhost" or (
                "." not in hostname
                or hostname.endswith((".internal", ".svc", ".cluster.local"))
            )
        if (
            parsed.scheme not in ({"http", "https"} if allow_http else {"https"})
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("llm_endpoint_invalid")
        if not model or len(model) > 200:
            raise ValueError("llm_model_invalid")
        token_parameter = (env.get("LLM_TOKEN_PARAMETER") or "auto").strip()
        if token_parameter == "auto":
            token_parameter = (
                "max_completion_tokens"
                if parsed.hostname.rstrip(".").lower() == "api.openai.com"
                else "max_tokens"
            )
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("llm_token_parameter_invalid")
        try:
            max_tokens = max(256, min(4096, int(env.get("LLM_MAX_TOKENS", "1536"))))
        except ValueError as exc:
            raise ValueError("llm_max_tokens_invalid") from exc
        try:
            timeout = max(10.0, min(60.0, float(env.get("LLM_TIMEOUT_SECONDS", "45"))))
            retries = max(0, min(2, int(env.get("LLM_RETRIES", "1"))))
        except ValueError as exc:
            raise ValueError("llm_budget_invalid") from exc
        return cls(
            api_key=key,
            endpoint=endpoint,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
            token_parameter=token_parameter,
            referer=(env.get("LLM_HTTP_REFERER") or "https://in-cypher.com")[:500],
            title=(env.get("LLM_APP_TITLE") or "INCYPHER Arena Agent")[:200],
        )


class ModelClient:
    def __init__(
        self,
        config: ModelConfiguration,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self.last_metrics: dict[str, int] = {}
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        time_budget: float | None = None,
    ) -> str:
        started = time.monotonic()
        deadline = (
            started + max(0.0, float(time_budget))
            if time_budget is not None
            else None
        )
        self.last_metrics = {}
        payload = {
            "model": self.config.model,
            "messages": list(messages),
            self.config.token_parameter: self.config.max_tokens,
        }
        endpoint_host = urllib.parse.urlsplit(self.config.endpoint).hostname
        if endpoint_host and endpoint_host.rstrip(".").lower() == "api.openai.com":
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "incypher-arena-agent/1.0",
            "HTTP-Referer": self.config.referer,
            "X-Title": self.config.title,
        }
        for attempt in range(self.config.retries + 1):
            attempts = attempt + 1
            timeout = self.config.timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._record_failure_metrics(started, attempt)
                    raise ModelFailure(
                        "llm_wall_budget_exhausted",
                        retryable=False,
                        attempts=attempt,
                    )
                timeout = min(timeout, max(0.001, remaining))
            request = urllib.request.Request(
                self.config.endpoint, data=body, method="POST", headers=headers
            )
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        self._record_failure_metrics(started, attempts)
                        raise ModelFailure(
                            "llm_response_too_large",
                            retryable=False,
                            attempts=attempts,
                        )
                    value = json.loads(raw.decode("utf-8"))
                content = value["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    self._record_failure_metrics(started, attempts)
                    raise ModelFailure(
                        "llm_response_missing_content",
                        retryable=False,
                        attempts=attempts,
                    )
                metrics = {
                    "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "transport_attempts": attempts,
                }
                usage = value.get("usage")
                if isinstance(usage, Mapping):
                    for source, destination in (
                        ("prompt_tokens", "prompt_tokens"),
                        ("completion_tokens", "completion_tokens"),
                        ("total_tokens", "total_tokens"),
                    ):
                        amount = usage.get(source)
                        if type(amount) is int and 0 <= amount <= 1_000_000_000:
                            metrics[destination] = amount
                self.last_metrics = metrics
                return content
            except urllib.error.HTTPError as exc:
                code, retryable = self._classify_http_failure(exc)
                failure = ModelFailure(
                    code,
                    retryable=retryable,
                    attempts=attempts,
                    status=exc.code,
                )
                if not retryable:
                    self._record_failure_metrics(started, attempts)
                    raise failure from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2.0 ** attempt
                if delay > 8.0:
                    self._record_failure_metrics(started, attempts)
                    raise failure from None
            except (urllib.error.URLError, TimeoutError):
                failure = ModelFailure(
                    "llm_network_error",
                    retryable=True,
                    attempts=attempts,
                )
                delay = min(8.0, 2.0 ** attempt)
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                self._record_failure_metrics(started, attempts)
                raise ModelFailure(
                    "llm_response_invalid",
                    retryable=False,
                    attempts=attempts,
                ) from None
            if attempt < self.config.retries:
                if deadline is not None and delay >= deadline - time.monotonic():
                    self._record_failure_metrics(started, attempts)
                    raise failure from None
                self._sleep(delay)
                continue
            self._record_failure_metrics(started, attempts)
            raise failure from None
        raise AssertionError("unreachable")

    def _record_failure_metrics(self, started: float, attempts: int) -> None:
        self.last_metrics = {
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            "transport_attempts": attempts,
        }

    @staticmethod
    def _classify_http_failure(
        error: urllib.error.HTTPError,
    ) -> tuple[str, bool]:
        status = error.code
        if status == 401:
            return "llm_authentication_failed", False
        if status == 403:
            return "llm_access_denied", False
        if status == 404:
            return "llm_endpoint_or_model_not_found", False
        if status == 429:
            provider_code = ""
            try:
                raw = error.read(65_537)
                if len(raw) <= 65_536:
                    value = json.loads(raw.decode("utf-8"))
                    detail = value.get("error") if isinstance(value, Mapping) else None
                    if isinstance(detail, Mapping):
                        provider_code = " ".join(
                            str(detail.get(field) or "")
                            for field in ("code", "type")
                        ).lower()
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            permanent_markers = (
                "quota", "billing", "credit", "spend_limit", "usage_limit"
            )
            if any(marker in provider_code for marker in permanent_markers):
                return "llm_quota_exhausted", False
            return "llm_rate_limited", True
        if status == 408:
            return "llm_request_timeout", True
        if 500 <= status <= 599:
            return "llm_service_unavailable", True
        return f"llm_http_{status}", False
