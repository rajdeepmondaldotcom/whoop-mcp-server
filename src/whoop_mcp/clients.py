"""Auto-configuration of MCP clients.

Used by ``whoop-mcp setup`` so a user never has to hand-edit JSON. One spec
per client; all JSON-file clients share a writer that is deliberately
conservative: existing config parsed leniently, a ``.bak`` written before
the first change (never overwritten with corrupt bytes), edits atomic.

Covered here: Claude Desktop, Cursor, Windsurf, VS Code (JSON configs) and
Claude Code (CLI registration). ChatGPT needs a remote URL and is documented
in the README instead.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SERVER_KEY = "whoop"


def find_binary() -> str:
    """Absolute path to the installed ``whoop-mcp`` command."""
    on_path = shutil.which("whoop-mcp")
    if on_path:
        return str(Path(on_path).resolve())
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "whoop-mcp" and candidate.exists():
        return str(candidate)
    sibling = Path(sys.executable).parent / "whoop-mcp"
    if sibling.exists():
        return str(sibling.resolve())
    return "whoop-mcp"  # hope it's on the client's PATH


# ------------------------------------------------------------ config paths


def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "~\\AppData\\Roaming")
        return Path(appdata).expanduser() / "Claude" / "claude_desktop_config.json"
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


def cursor_config_path() -> Path:
    return Path("~/.cursor/mcp.json").expanduser()


def windsurf_config_path() -> Path:
    return Path("~/.codeium/windsurf/mcp_config.json").expanduser()


def vscode_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path("~/Library/Application Support/Code/User/mcp.json").expanduser()
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "~\\AppData\\Roaming")
        return Path(appdata).expanduser() / "Code" / "User" / "mcp.json"
    return Path("~/.config/Code/User/mcp.json").expanduser()


# ------------------------------------------------------------ client specs


@dataclass(frozen=True)
class ClientSpec:
    key: str
    name: str
    config_path: Callable[[], Path]
    container_key: str  # "mcpServers" (most clients) or "servers" (VS Code)
    entry_style: str  # "plain" -> {command,args}; "typed" -> {type,command,args}
    restart_hint: str


CLIENT_SPECS: tuple[ClientSpec, ...] = (
    ClientSpec(
        key="claude-desktop",
        name="Claude Desktop",
        config_path=claude_desktop_config_path,
        container_key="mcpServers",
        entry_style="plain",
        restart_hint="Fully quit and reopen Claude Desktop to load it.",
    ),
    ClientSpec(
        key="cursor",
        name="Cursor",
        config_path=cursor_config_path,
        container_key="mcpServers",
        entry_style="plain",
        restart_hint="Restart Cursor (or toggle the server in Settings → MCP).",
    ),
    ClientSpec(
        key="windsurf",
        name="Windsurf",
        config_path=windsurf_config_path,
        container_key="mcpServers",
        entry_style="plain",
        restart_hint="Refresh the plugins list in Windsurf's Cascade panel.",
    ),
    ClientSpec(
        key="vscode",
        name="VS Code",
        config_path=vscode_config_path,
        container_key="servers",
        entry_style="typed",
        restart_hint="Run “MCP: List Servers” in VS Code to start it.",
    ),
)


def detected_clients() -> list[ClientSpec]:
    """Specs whose config directory exists on this machine."""
    return [spec for spec in CLIENT_SPECS if spec.config_path().parent.exists()]


def server_entry(spec: ClientSpec, binary: str) -> dict:
    if spec.entry_style == "typed":
        return {"type": "stdio", "command": binary, "args": ["serve"]}
    return {"command": binary, "args": ["serve"]}


def install_into(spec: ClientSpec, binary: str | None = None) -> tuple[Path, str]:
    """Add (or update) the whoop entry in one client's config.

    Returns (config_path, action) where action is "added" | "updated" |
    "unchanged".
    """
    binary = binary or find_binary()
    path = spec.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    original_corrupt = False
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except (OSError, json.JSONDecodeError):
            # Keep the broken original safe and start fresh.
            original_corrupt = True
            shutil.copy2(path, path.with_suffix(".json.broken"))

    servers = config.setdefault(spec.container_key, {})
    desired = server_entry(spec, binary)
    existing = servers.get(SERVER_KEY)
    if existing == desired:
        return path, "unchanged"
    action = "updated" if existing else "added"
    servers[SERVER_KEY] = desired

    # Never let a corrupt original overwrite the last *good* backup — the
    # .json.broken copy above already preserves the corrupt bytes.
    if path.exists() and not original_corrupt:
        shutil.copy2(path, path.with_suffix(".json.bak"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path, action


def claude_desktop_detected() -> bool:
    return claude_desktop_config_path().parent.exists()


def install_into_claude_desktop(binary: str | None = None) -> tuple[Path, str]:
    """Back-compat wrapper around :func:`install_into` for Claude Desktop."""
    spec = ClientSpec(
        key="claude-desktop",
        name="Claude Desktop",
        config_path=claude_desktop_config_path,
        container_key="mcpServers",
        entry_style="plain",
        restart_hint="Fully quit and reopen Claude Desktop to load it.",
    )
    return install_into(spec, binary)


# ------------------------------------------------------------- Claude Code


def claude_code_detected() -> bool:
    return shutil.which("claude") is not None


def claude_code_command(binary: str | None = None) -> list[str]:
    binary = binary or find_binary()
    return ["claude", "mcp", "add", "--scope", "user", SERVER_KEY, "--", binary, "serve"]
