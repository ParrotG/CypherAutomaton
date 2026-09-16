"""Secure loading of the IN-CYPHER team key and local ``.env.local`` values.

The key is used only inside this process for the HMAC proof-of-work.  It is
never sent on the wire and is never written into a transcript.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable, Mapping

ENV_NAMES = ("CYPHER_TEAM_KEY", "INCYPHER_TEAM_KEY", "TEAM_KEY")
DEFAULT_ENV_FILE = ".env.local"
DEFAULT_KEY_FILE = "~/.config/incypher/team_key"


class TeamKeyError(RuntimeError):
    """Raised when the team key cannot be loaded securely."""


def _check_private_file(path: Path, *, allow_insecure: bool, label: str) -> None:
    if not path.exists():
        raise TeamKeyError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise TeamKeyError(f"{label} is not a regular file: {path}")
    if os.name == "posix" and not allow_insecure:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise TeamKeyError(
                f"{label} {path} has insecure permissions {mode:03o}; "
                "run `chmod 600` or pass --allow-insecure-key-file"
            )


def parse_env_file(text: str) -> dict[str, str]:
    """Tiny ``.env`` parser.

    Supports comments, blank lines, optional ``export``, single/double quotes,
    and does not perform shell expansion.  Later entries win, matching the
    usual dotenv behaviour.
    """

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def read_env_file(
    path: Path,
    *,
    allow_insecure_file: bool = False,
    required: bool = False,
) -> Mapping[str, str]:
    """Read a private env file after a permission check."""

    if not path.exists():
        if required:
            raise TeamKeyError(f"env file does not exist: {path}")
        return {}
    _check_private_file(path, allow_insecure=allow_insecure_file, label="env file")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TeamKeyError(f"cannot read env file {path}: {exc}") from exc
    return parse_env_file(text)


def apply_env_file(path: Path, *, allow_insecure_file: bool = False) -> Mapping[str, str]:
    """Load *path* into ``os.environ`` without overriding real environment vars."""

    values = read_env_file(path, allow_insecure_file=allow_insecure_file)
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
    return values


def _read_key_file(path: Path, *, allow_insecure_file: bool) -> str:
    _check_private_file(path, allow_insecure=allow_insecure_file, label="team-key file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TeamKeyError(f"cannot read team-key file {path}: {exc}") from exc
    if not value:
        raise TeamKeyError(f"team-key file is empty: {path}")
    return value


def _candidate_key_files() -> Iterable[Path]:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        yield Path(xdg_config).expanduser() / "incypher" / "team_key"
    yield Path(DEFAULT_KEY_FILE).expanduser()


def load_team_key(
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    *,
    explicit_file: str | Path | None = None,
    allow_insecure_file: bool = False,
) -> str:
    """Load the team key from the environment, env file, or a private key file.

    Priority:
      1. ``--team-key-file`` / ``INCYPHER_TEAM_KEY_FILE`` if explicitly given.
      2. Existing process environment variables.
      3. The requested ``.env.local`` file (default ``./.env.local``).
      4. ``~/.config/incypher/team_key`` (or ``$XDG_CONFIG_HOME``).
    """

    if explicit_file is None:
        for env_name in ("INCYPHER_TEAM_KEY_FILE", "CYPHER_TEAM_KEY_FILE", "TEAM_KEY_FILE"):
            value = os.environ.get(env_name)
            if value and value.strip():
                explicit_file = value.strip()
                break
    if explicit_file is not None:
        return _read_key_file(Path(explicit_file).expanduser(), allow_insecure_file=allow_insecure_file)

    env_path = Path(env_file).expanduser() if env_file is not None else None
    # Existing environment always wins over file contents.
    for name in ENV_NAMES:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()

    if env_path is not None and env_path.exists():
        apply_env_file(env_path, allow_insecure_file=allow_insecure_file)
        for name in ENV_NAMES:
            value = os.environ.get(name)
            if value and value.strip():
                return value.strip()

    errors: list[str] = []
    for path in _candidate_key_files():
        if not path.exists():
            continue
        try:
            return _read_key_file(path, allow_insecure_file=allow_insecure_file)
        except TeamKeyError as exc:
            errors.append(str(exc))

    detail = "; ".join(errors) if errors else "no key source found"
    raise TeamKeyError(
        "Could not load the IN-CYPHER team key. Put CYPHER_TEAM_KEY=... in a "
        f"mode-0600 {DEFAULT_ENV_FILE}, set CYPHER_TEAM_KEY in the environment, "
        f"or create a mode-0600 {Path(DEFAULT_KEY_FILE).expanduser()}. ({detail})"
    )


def find_env_file(cli_value: str | None = None) -> Path:
    """Return the env-file path selected by the CLI or the default."""

    return Path(cli_value or DEFAULT_ENV_FILE).expanduser()
