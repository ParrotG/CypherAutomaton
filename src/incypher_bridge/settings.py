"""Runtime settings for the FastAPI bridge service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import TeamKeyError, find_env_file, load_team_key


class SettingsError(RuntimeError):
    """Raised when bridge settings cannot be loaded."""


@dataclass(frozen=True)
class BridgeSettings:
    state_root: Path
    team_key: str
    env_file: str
    max_handshakes: int = 4
    api_host: str = "127.0.0.1"
    api_port: int = 8765

    @property
    def connectors_root(self) -> Path:
        return self.state_root / "connectors"


def load_settings(
    *,
    state_dir: str | Path = ".cypher_bridge/bridge",
    env_file: str | None = ".env.local",
    team_key_file: str | None = None,
    allow_insecure_file: bool = False,
    max_handshakes: int = 4,
    api_host: str = "127.0.0.1",
    api_port: int = 8765,
) -> BridgeSettings:
    if max_handshakes < 1:
        raise SettingsError("max_handshakes must be positive")
    selected_env_file = find_env_file(env_file)
    try:
        team_key = load_team_key(
            env_file=selected_env_file,
            explicit_file=team_key_file,
            allow_insecure_file=allow_insecure_file,
        )
    except TeamKeyError as exc:
        raise SettingsError(str(exc)) from exc
    state_root = Path(state_dir).expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    try:
        (state_root / "connectors").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsError(f"cannot create state directory {state_root}: {exc}") from exc
    return BridgeSettings(
        state_root=state_root,
        team_key=team_key,
        env_file=str(selected_env_file),
        max_handshakes=max_handshakes,
        api_host=api_host,
        api_port=api_port,
    )
