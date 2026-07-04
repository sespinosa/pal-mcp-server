"""Tests for named clink client profiles and user override loading."""

import json

import pytest

from clink.agents import create_agent
from clink.agents.claude import ClaudeAgent
from clink.registry import ClinkRegistry, RegistryLoadError


def test_profile_inherits_runner_defaults(monkeypatch, tmp_path):
    config = {
        "name": "claude-work",
        "command": "claude",
        "runner": "claude",
        "additional_args": ["--permission-mode", "acceptEdits", "--model", "sonnet"],
        "env": {"CLAUDE_CONFIG_DIR": "/home/user/.claude-work"},
    }
    config_path = tmp_path / "claude-work.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))
    # Keep the developer's real ~/.pal overrides out of this test.
    monkeypatch.setattr("clink.registry.USER_CONFIG_DIR", tmp_path / "user-overrides")

    registry = ClinkRegistry()
    client = registry.get_client("claude-work")

    # Inherits the built-in claude internals under its own name.
    assert client.parser == "claude_json"
    assert client.runner == "claude"
    assert client.internal_args == ["--print", "--output-format", "json"]
    assert client.env["CLAUDE_CONFIG_DIR"] == "/home/user/.claude-work"
    assert client.get_role("default").prompt_path.name == "default.txt"
    assert isinstance(create_agent(client), ClaudeAgent)

    # The built-in claude client is untouched.
    assert registry.get_client("claude").env == {}


def test_unknown_runner_rejected(monkeypatch, tmp_path):
    config_path = tmp_path / "bad.json"
    config_path.write_text(json.dumps({"name": "bad", "command": "bad", "runner": "nope"}))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    with pytest.raises(RegistryLoadError, match="unknown runner"):
        ClinkRegistry()


def test_unknown_name_without_runner_rejected(monkeypatch, tmp_path):
    config_path = tmp_path / "mystery.json"
    config_path.write_text(json.dumps({"name": "mystery", "command": "mystery"}))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    with pytest.raises(RegistryLoadError, match="not supported"):
        ClinkRegistry()


def test_broken_user_override_is_skipped(monkeypatch, tmp_path, caplog):
    """A broken auto-discovered ~/.pal override must not take down the whole registry."""
    user_dir = tmp_path / "user-overrides"
    user_dir.mkdir()
    (user_dir / "broken.json").write_text(json.dumps({"name": "future", "command": "x"}))
    (user_dir / "good.json").write_text(json.dumps({"name": "claude-work", "command": "claude", "runner": "claude"}))
    monkeypatch.delenv("CLI_CLIENTS_CONFIG_PATH", raising=False)
    monkeypatch.setattr("clink.registry.USER_CONFIG_DIR", user_dir)

    with caplog.at_level("WARNING", logger="clink.registry"):
        registry = ClinkRegistry()

    # Built-ins and the valid override load; the broken file is skipped with a warning.
    assert "claude-work" in registry.list_clients()
    assert "gemini" in registry.list_clients()
    assert any("broken.json" in record.message for record in caplog.records)


def test_broken_explicit_config_path_still_fatal(monkeypatch, tmp_path):
    """CLI_CLIENTS_CONFIG_PATH is explicitly requested, so errors there stay fatal."""
    config_path = tmp_path / "broken.json"
    config_path.write_text(json.dumps({"name": "future", "command": "x"}))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    with pytest.raises(RegistryLoadError, match="not supported"):
        ClinkRegistry()
