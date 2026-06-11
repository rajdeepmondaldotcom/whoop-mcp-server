"""Configuration for whoop-mcp.

Values are resolved in priority order:

1. Process environment variables (``WHOOP_*``)
2. A ``.env`` file in the current working directory
3. A ``.env`` file in the data directory (``~/.whoop-mcp`` by default)
4. ``config.json`` in the data directory (written by ``whoop-mcp auth --save``)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE_URL = "https://api.prod.whoop.com/developer"

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_DATA_DIR = "~/.whoop-mcp"

# Every read scope WHOOP offers, plus `offline` so we get a refresh token.
DEFAULT_SCOPES: tuple[str, ...] = (
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
    "offline",
)


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_DATA_DIR).expanduser())
    timezone: str | None = None
    cache_ttl: float = 60.0
    request_timeout: float = 30.0
    log_level: str = "INFO"
    # Set via WHOOP_ACCESS_TOKEN to bypass OAuth entirely (no refresh; for testing).
    static_access_token: str | None = None

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    def require_oauth_app(self) -> None:
        """Raise ConfigError unless a WHOOP app client id/secret is configured."""
        from whoop_mcp.errors import ConfigError

        missing = []
        if not self.client_id:
            missing.append("WHOOP_CLIENT_ID")
        if not self.client_secret:
            missing.append("WHOOP_CLIENT_SECRET")
        if missing:
            raise ConfigError(
                f"Missing {' and '.join(missing)}. Create an app at "
                "https://developer-dashboard.whoop.com, then either export the "
                "environment variables or run `whoop-mcp auth` once with "
                "--client-id/--client-secret to store them."
            )


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env file. Quotes and comments are handled."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _read_config_json(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Build Settings from the environment plus optional .env / config.json files."""
    env = dict(environ if environ is not None else os.environ)

    def merge(extra: dict[str, str]) -> None:
        for key, value in extra.items():
            env.setdefault(key, value)

    merge(_read_env_file(Path.cwd() / ".env"))
    data_dir = Path(env.get("WHOOP_MCP_DIR", DEFAULT_DATA_DIR)).expanduser()
    merge(_read_env_file(data_dir / ".env"))
    merge(_read_config_json(data_dir / "config.json"))

    scopes_raw = env.get("WHOOP_SCOPES", " ".join(DEFAULT_SCOPES))
    scopes = tuple(s for s in scopes_raw.replace(",", " ").split() if s)

    def as_float(name: str, default: float) -> float:
        try:
            return float(env.get(name, default))
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric %s", name)
            return default

    return Settings(
        client_id=env.get("WHOOP_CLIENT_ID") or None,
        client_secret=env.get("WHOOP_CLIENT_SECRET") or None,
        redirect_uri=env.get("WHOOP_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        scopes=scopes,
        data_dir=data_dir,
        timezone=env.get("WHOOP_MCP_TZ") or None,
        cache_ttl=as_float("WHOOP_MCP_CACHE_TTL", 60.0),
        request_timeout=as_float("WHOOP_MCP_TIMEOUT", 30.0),
        log_level=env.get("WHOOP_MCP_LOG_LEVEL", "INFO").upper(),
        static_access_token=env.get("WHOOP_ACCESS_TOKEN") or None,
    )


def save_credentials(
    data_dir: Path,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str | None = None,
) -> Path:
    """Persist app credentials to ``config.json`` (created 0600) for later runs."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = data_dir / "config.json"
    existing = _read_config_json(path)
    existing.update(
        {
            "WHOOP_CLIENT_ID": client_id,
            "WHOOP_CLIENT_SECRET": client_secret,
        }
    )
    if redirect_uri:
        existing["WHOOP_REDIRECT_URI"] = redirect_uri
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return path
