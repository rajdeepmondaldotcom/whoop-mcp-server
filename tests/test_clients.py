"""Tests for the Claude Desktop / Claude Code auto-configuration helpers."""

import json

from whoop_mcp import clients


def test_install_into_fresh_config(tmp_path, monkeypatch):
    config_path = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(clients, "claude_desktop_config_path", lambda: config_path)

    path, action = clients.install_into_claude_desktop("/usr/local/bin/whoop-mcp")
    assert action == "added"
    config = json.loads(path.read_text())
    assert config["mcpServers"]["whoop"] == {
        "command": "/usr/local/bin/whoop-mcp",
        "args": ["serve"],
    }


def test_install_preserves_existing_servers_and_backs_up(tmp_path, monkeypatch):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"})
    )
    monkeypatch.setattr(clients, "claude_desktop_config_path", lambda: config_path)

    path, action = clients.install_into_claude_desktop("/bin/whoop-mcp")
    assert action == "added"
    config = json.loads(path.read_text())
    assert config["mcpServers"]["other"] == {"command": "x"}  # untouched
    assert config["theme"] == "dark"
    assert config["mcpServers"]["whoop"]["command"] == "/bin/whoop-mcp"
    assert path.with_suffix(".json.bak").exists()

    # Second run with the same binary is a no-op.
    _, action = clients.install_into_claude_desktop("/bin/whoop-mcp")
    assert action == "unchanged"

    # A different binary updates in place.
    _, action = clients.install_into_claude_desktop("/elsewhere/whoop-mcp")
    assert action == "updated"


def test_install_survives_corrupt_config(tmp_path, monkeypatch):
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text("{not valid json")
    # A good backup from an earlier run must never be clobbered by corrupt bytes.
    good_backup = config_path.with_suffix(".json.bak")
    good_backup.write_text(json.dumps({"mcpServers": {"precious": {"command": "x"}}}))
    monkeypatch.setattr(clients, "claude_desktop_config_path", lambda: config_path)

    path, action = clients.install_into_claude_desktop("/bin/whoop-mcp")
    assert action == "added"
    assert json.loads(path.read_text())["mcpServers"]["whoop"]
    assert path.with_suffix(".json.broken").exists()  # corrupt original kept
    assert json.loads(good_backup.read_text())["mcpServers"]["precious"]  # untouched


def test_claude_code_command_shape():
    command = clients.claude_code_command("/bin/whoop-mcp")
    assert command[:4] == ["claude", "mcp", "add", "--scope"]
    assert command[-3:] == ["--", "/bin/whoop-mcp", "serve"]


def test_vscode_layout_uses_servers_and_type(tmp_path):
    spec = clients.ClientSpec(
        key="vscode",
        name="VS Code",
        config_path=lambda: tmp_path / "User" / "mcp.json",
        container_key="servers",
        entry_style="typed",
        restart_hint="",
    )
    path, action = clients.install_into(spec, "/bin/whoop-mcp")
    assert action == "added"
    config = json.loads(path.read_text())
    assert config["servers"]["whoop"] == {
        "type": "stdio",
        "command": "/bin/whoop-mcp",
        "args": ["serve"],
    }


def test_all_client_specs_have_distinct_paths():
    paths = {spec.config_path() for spec in clients.CLIENT_SPECS}
    assert len(paths) == len(clients.CLIENT_SPECS)
