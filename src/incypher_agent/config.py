"""Configuration and workspace discovery for the minimal agent unit."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incypher_bridge.config import TeamKeyError, read_env_file
from incypher_bridge.paths import resolve_run_dir

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_ENV_FILE = ".env.local"
DEFAULT_STATE_DIR = ".cypher_bridge"
DEFAULT_FLAG_PATTERN = r"(?:flag|INCYPHER)\{[^}\r\n]+\}"
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000
DEFAULT_CONTEXT_RESERVE_TOKENS = 8_000


class AgentConfigError(RuntimeError):
    """Raised when the agent unit cannot be configured safely."""


def validate_api_key(value: str) -> str:
    """Validate the API key loaded from ``.env.local``."""

    key = (value or "").strip()
    if len(key) < 16 or any(char.isspace() for char in key):
        raise AgentConfigError(
            "DEEPSEEK_API_KEY is missing or invalid in .env.local"
        )
    return key


@dataclass
class AgentConfig:
    # Bridge / challenge context
    challenge_id: str
    run_id: str
    run_dir: Path
    workspace_dir: Path
    agent_endpoint: str | None = None
    challenge_name: str | None = None
    category: str | None = None
    description_text: str | None = None
    challenge_md: str | None = None
    target: str | None = None
    # Model provider
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    thinking: str | None = None
    temperature: float | None = None
    # Runtime limits
    max_seconds: float = 3600.0
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    context_reserve_tokens: int = DEFAULT_CONTEXT_RESERVE_TOKENS
    bash_timeout: float = 60.0
    max_tool_output: int = 20_000
    flag_pattern: str = DEFAULT_FLAG_PATTERN
    verbose: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self, *, require_api_key: bool = True) -> None:
        if require_api_key:
            validate_api_key(self.api_key)
        if not self.run_dir.exists():
            raise AgentConfigError(f"run directory does not exist: {self.run_dir}")
        if self.max_seconds <= 0:
            raise AgentConfigError("max_seconds must be positive")
        if self.context_window_tokens <= 0:
            raise AgentConfigError("context_window_tokens must be positive")
        if self.context_reserve_tokens < 0:
            raise AgentConfigError("context_reserve_tokens must be non-negative")
        if self.context_window_tokens <= self.context_reserve_tokens:
            raise AgentConfigError(
                "context_window_tokens must be greater than context_reserve_tokens"
            )
        if self.bash_timeout <= 0:
            raise AgentConfigError("bash_timeout must be positive")
        if self.max_tool_output <= 0:
            raise AgentConfigError("max_tool_output must be positive")


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_env_values(
    env_file: str | Path | None = None,
    *,
    allow_insecure_file: bool = False,
    required: bool = True,
) -> dict[str, str]:
    """Load agent settings from the selected private env file only."""

    env_path = Path(env_file or DEFAULT_ENV_FILE).expanduser()
    try:
        return dict(
            read_env_file(
                env_path,
                allow_insecure_file=allow_insecure_file,
                required=required,
            )
        )
    except TeamKeyError as exc:
        raise AgentConfigError(str(exc)) from exc


def load_run_context(run_dir: Path) -> dict[str, Any]:
    """Read the bridge run.json and fill in derived description fields."""

    run_file = run_dir / "run.json"
    try:
        import json

        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentConfigError(f"cannot read run metadata: {run_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentConfigError(f"run metadata is not an object: {run_file}")
    return payload


def build_agent_config(
    *,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    challenge_id: str | None = None,
    run_id: str | None = None,
    run_dir: str | Path | None = None,
    workspace_dir: str | Path | None = None,
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    allow_insecure_file: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    thinking: str | None = None,
    temperature: float | None = None,
    max_seconds: float = 3600.0,
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    context_reserve_tokens: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
    bash_timeout: float = 60.0,
    max_tool_output: int = 20_000,
    flag_pattern: str = DEFAULT_FLAG_PATTERN,
    verbose: bool = False,
    require_api_key: bool = True,
) -> AgentConfig:
    """Resolve bridge context and model provider settings.

    ``run_dir`` may be supplied directly; otherwise it is resolved from
    ``challenge_id`` / ``run_id`` or the active run pointer.
    """

    env_values = load_env_values(
        env_file,
        allow_insecure_file=allow_insecure_file,
        required=require_api_key,
    )

    if run_dir is not None:
        resolved_run_dir = Path(run_dir).expanduser().resolve()
    else:
        resolved_run_dir = resolve_run_dir(
            Path(state_dir), challenge_id=challenge_id, run_id=run_id
        )

    context = load_run_context(resolved_run_dir)
    resolved_challenge_id = (
        challenge_id or context.get("challenge_id") or resolved_run_dir.parent.parent.name
    )
    resolved_run_id = run_id or context.get("run_id") or resolved_run_dir.name

    description_text = context.get("description_text")
    challenge_md = context.get("challenge_md")
    if not description_text and challenge_md and Path(challenge_md).exists():
        description_text = _read_text_file(Path(challenge_md))

    resolved_workspace = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir is not None
        else resolved_run_dir / "agent" / "workspace"
    )
    resolved_workspace.mkdir(parents=True, exist_ok=True)

    resolved_model = model or DEFAULT_MODEL
    resolved_base_url = (
        base_url
        or env_values.get("DEEPSEEK_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    resolved_api_key = env_values.get("DEEPSEEK_API_KEY", "").strip()
    resolved_thinking = (
        thinking or env_values.get("CYPHER_THINKING", "").strip() or None
    )

    config = AgentConfig(
        challenge_id=str(resolved_challenge_id),
        run_id=str(resolved_run_id),
        run_dir=resolved_run_dir,
        workspace_dir=resolved_workspace,
        agent_endpoint=context.get("agent_endpoint"),
        challenge_name=context.get("challenge_name"),
        category=context.get("category"),
        description_text=description_text,
        challenge_md=str(challenge_md) if challenge_md else None,
        target=context.get("target"),
        model=resolved_model,
        base_url=resolved_base_url or DEFAULT_BASE_URL,
        api_key=resolved_api_key or "",
        thinking=resolved_thinking,
        temperature=temperature,
        max_seconds=float(max_seconds),
        context_window_tokens=int(context_window_tokens),
        context_reserve_tokens=int(context_reserve_tokens),
        bash_timeout=float(bash_timeout),
        max_tool_output=int(max_tool_output),
        flag_pattern=flag_pattern,
        verbose=verbose,
        metadata={"bridge_state": context.get("state")},
    )
    config.validate(require_api_key=require_api_key)
    return config
