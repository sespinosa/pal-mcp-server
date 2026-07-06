"""Tests for the inter-agent mailbox: message store and tool actions."""

import json

import pytest

from tools.mailbox import MailboxTool
from tools.shared.exceptions import ToolExecutionError
from utils.mailbox import (
    MailboxError,
    claim_messages,
    drain_messages,
    list_stack,
    mailbox_root,
    peek_messages,
    register_agent,
    send_message,
    uniquify_agent_id,
    unregister_agent,
)


@pytest.fixture
def mailbox_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAL_MAILBOX_DIR", str(tmp_path / "mailbox"))
    monkeypatch.setenv("PAL_AGENT_ID", "worker-1")
    return tmp_path


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------


def test_send_and_drain_roundtrip(mailbox_env):
    send_message(sender="parent", recipient="worker-1", text="first")
    send_message(sender="parent", recipient="worker-1", text="second")

    messages = drain_messages("worker-1")
    assert [m.text for m in messages] == ["first", "second"]
    assert messages[0].sender == "parent"

    # Draining claims the messages: a second drain is empty.
    assert drain_messages("worker-1") == []


def test_drain_unknown_agent_is_empty(mailbox_env):
    assert drain_messages("nobody") == []


def test_messages_are_isolated_per_recipient(mailbox_env):
    send_message(sender="a", recipient="worker-1", text="for one")
    send_message(sender="a", recipient="worker-2", text="for two")

    assert [m.text for m in drain_messages("worker-2")] == ["for two"]
    assert [m.text for m in drain_messages("worker-1")] == ["for one"]


@pytest.mark.parametrize("bad_id", ["", "../escape", "a/b", ".hidden", "x" * 65, "worker.", "CON", "com1.log"])
def test_invalid_agent_ids_rejected(mailbox_env, bad_id):
    with pytest.raises(MailboxError):
        send_message(sender="parent", recipient=bad_id, text="hi")
    with pytest.raises(MailboxError):
        drain_messages(bad_id)


def test_empty_message_rejected(mailbox_env):
    with pytest.raises(MailboxError):
        send_message(sender="parent", recipient="worker-1", text="   ")


def test_peek_does_not_claim_until_asked(mailbox_env):
    send_message(sender="parent", recipient="worker-1", text="two-phase")

    entries = peek_messages("worker-1")
    assert [m.text for _, m in entries] == ["two-phase"]
    # Still pending after peek; gone after explicit claim.
    assert [m.text for _, m in peek_messages("worker-1")] == ["two-phase"]
    claim_messages([path for path, _ in entries])
    assert peek_messages("worker-1") == []


def test_malformed_message_files_are_removed(mailbox_env):
    send_message(sender="parent", recipient="worker-1", text="good")
    spool = mailbox_root() / "worker-1"
    poison = spool / "0000000000000000000-poison.json"
    poison.write_text("{not json", encoding="utf-8")

    messages = drain_messages("worker-1")
    assert [m.text for m in messages] == ["good"]
    assert not poison.exists()


# ----------------------------------------------------------------------
# Tool actions
# ----------------------------------------------------------------------


async def _run_tool(arguments):
    result = await MailboxTool().execute(arguments)
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_tool_send_and_check(mailbox_env, monkeypatch):
    monkeypatch.setenv("PAL_AGENT_ID", "worker-2")
    sent = await _run_tool({"action": "send", "to": "worker-1", "message": "ping from two"})
    assert sent["status"] == "success"
    assert sent["metadata"]["from"] == "worker-2"

    monkeypatch.setenv("PAL_AGENT_ID", "worker-1")
    checked = await _run_tool({"action": "check"})
    assert checked["metadata"]["message_count"] == 1
    assert "ping from two" in checked["content"]

    # Consumed on read.
    rechecked = await _run_tool({"action": "check"})
    assert rechecked["metadata"]["message_count"] == 0


@pytest.mark.asyncio
async def test_tool_defaults_to_parent_identity(mailbox_env, monkeypatch):
    monkeypatch.delenv("PAL_AGENT_ID")
    await _run_tool({"action": "send", "to": "parent", "message": "note to self"})
    checked = await _run_tool({"action": "check"})
    assert checked["metadata"]["agent_id"] == "parent"
    assert checked["metadata"]["message_count"] == 1


@pytest.mark.asyncio
async def test_tool_send_requires_recipient(mailbox_env):
    with pytest.raises(ToolExecutionError):
        await _run_tool({"action": "send", "message": "no recipient"})


@pytest.mark.asyncio
async def test_tool_rejects_unknown_action(mailbox_env):
    with pytest.raises(ToolExecutionError):
        await _run_tool({"action": "destroy"})


# ----------------------------------------------------------------------
# Roster: stack-scoped registry of live spawned agents
# ----------------------------------------------------------------------


def test_roster_register_list_unregister(mailbox_env):
    register_agent("worker-a", parent_id="parent", client="claude", role="default")
    register_agent("worker-b", parent_id="parent", client="codex", role="planner")
    register_agent("grandchild", parent_id="worker-a", client="claude", role="default")

    stack = list_stack("parent")
    assert sorted(w["agent_id"] for w in stack["workers"]) == ["worker-a", "worker-b"]
    assert stack["parent_id"] is None  # parent itself was not spawned by clink

    # A worker sees its own entry, its parent, and only its own workers.
    worker_stack = list_stack("worker-a")
    assert worker_stack["parent_id"] == "parent"
    assert [w["agent_id"] for w in worker_stack["workers"]] == ["grandchild"]

    unregister_agent("worker-a")
    assert sorted(w["agent_id"] for w in list_stack("parent")["workers"]) == ["worker-b"]
    unregister_agent("worker-a")  # already gone is fine


def test_uniquify_agent_id_suffixes_live_ids(mailbox_env):
    assert uniquify_agent_id("worker") == "worker"
    register_agent("worker", parent_id="parent", client="claude", role="default")
    assert uniquify_agent_id("worker") == "worker-2"
    register_agent("worker-2", parent_id="parent", client="claude", role="default")
    assert uniquify_agent_id("worker") == "worker-3"
    unregister_agent("worker")
    assert uniquify_agent_id("worker") == "worker"


def test_stale_roster_entries_expire(mailbox_env, monkeypatch):
    register_agent("old-worker", parent_id="parent", client="claude", role="default")
    import utils.mailbox as mailbox_module

    far_future = mailbox_module.time.time() + 10**9
    monkeypatch.setattr(mailbox_module.time, "time", lambda: far_future)
    assert list_stack("parent")["workers"] == []


@pytest.mark.asyncio
async def test_tool_list_action_scopes_to_own_stack(mailbox_env, monkeypatch):
    register_agent("mine", parent_id="worker-1", client="claude", role="default")
    register_agent("not-mine", parent_id="someone-else", client="claude", role="default")

    result = await _run_tool({"action": "list"})
    assert result["metadata"]["worker_count"] == 1
    assert "'mine'" in result["content"]
    assert "not-mine" not in result["content"]


# ----------------------------------------------------------------------
# clink integration: spawned workers learn about their mailbox
# ----------------------------------------------------------------------


def _clink_capture_prompt(monkeypatch, tmp_path):
    """Run clink with a dummy agent (hermetic registry) and capture the spawned prompt."""
    from clink.agents import AgentOutput
    from clink.parsers.base import ParsedCLIResponse
    from clink.registry import ClinkRegistry

    # Built-in configs only: keep the developer's real ~/.pal overrides out of the test.
    monkeypatch.delenv("CLI_CLIENTS_CONFIG_PATH", raising=False)
    monkeypatch.setattr("clink.registry.USER_CONFIG_DIR", tmp_path / "user-overrides")
    registry = ClinkRegistry()
    monkeypatch.setattr("tools.clink.get_registry", lambda: registry)

    from tools.clink import CLinkTool

    captured = {}

    class DummyAgent:
        async def run(self, **kwargs):
            captured["prompt"] = kwargs["prompt"]
            from utils.mailbox import current_agent_id as _current
            from utils.mailbox import list_stack as _list_stack

            captured["workers_during_run"] = _list_stack(_current())["workers"]
            return AgentOutput(
                parsed=ParsedCLIResponse(content="ok", metadata={}),
                sanitized_command=["gemini"],
                returncode=0,
                stdout="{}",
                stderr="",
                duration_seconds=0.1,
                parser_name="gemini_json",
                output_file_content=None,
            )

    monkeypatch.setattr("tools.clink.create_agent", lambda client: DummyAgent())
    return CLinkTool(), captured, registry


@pytest.mark.asyncio
async def test_clink_briefs_worker_when_agent_id_configured(mailbox_env, monkeypatch, tmp_path):
    tool, captured, registry = _clink_capture_prompt(monkeypatch, tmp_path)
    client = registry.get_client("gemini")
    monkeypatch.setitem(client.env, "PAL_AGENT_ID", "worker-9")

    await tool.execute({"prompt": "do the thing", "cli_name": "gemini"})

    assert "=== PAL MAILBOX ===" in captured["prompt"]
    assert "your agent id is 'worker-9'" in captured["prompt"]
    # The spawner's own id (from the mailbox_env fixture) is named as the parent.
    assert "spawned you is 'worker-1'" in captured["prompt"]


@pytest.mark.asyncio
async def test_clink_prompt_unchanged_without_agent_id(mailbox_env, monkeypatch, tmp_path):
    tool, captured, _registry = _clink_capture_prompt(monkeypatch, tmp_path)

    await tool.execute({"prompt": "do the thing", "cli_name": "gemini"})

    assert "PAL MAILBOX" not in captured["prompt"]


@pytest.mark.asyncio
async def test_clink_registers_worker_and_uniquifies_id(mailbox_env, monkeypatch, tmp_path):
    import json as json_module

    tool, captured, registry = _clink_capture_prompt(monkeypatch, tmp_path)
    client = registry.get_client("gemini")
    monkeypatch.setitem(client.env, "PAL_AGENT_ID", "worker-9")
    # Another live agent already holds the base id, e.g. a concurrent spawn.
    register_agent("worker-9", parent_id="worker-1", client="gemini", role="default")

    results = await tool.execute({"prompt": "do the thing", "cli_name": "gemini"})
    payload = json_module.loads(results[0].text)

    # The spawn got a unique instance id, surfaced to the parent and briefed to the worker.
    assert payload["metadata"]["mailbox_agent_id"] == "worker-9-2"
    assert "your agent id is 'worker-9-2'" in captured["prompt"]
    assert "spawned you is 'worker-1'" in captured["prompt"]
    # Roster contained it during the run and is clean afterwards.
    during = {w["agent_id"]: w for w in captured["workers_during_run"]}
    assert during["worker-9-2"]["parent_id"] == "worker-1"
    assert [w["agent_id"] for w in list_stack("worker-1")["workers"]] == ["worker-9"]


def test_mailbox_instructions_appended_only_when_enabled():
    from server import MAILBOX_INSTRUCTIONS, augment_instructions_for_mailbox

    base = "base instructions"
    with_mailbox = augment_instructions_for_mailbox(base, {"mailbox": object(), "chat": object()})
    assert with_mailbox.startswith(base)
    assert MAILBOX_INSTRUCTIONS in with_mailbox

    without_mailbox = augment_instructions_for_mailbox(base, {"chat": object()})
    assert without_mailbox == base


# ----------------------------------------------------------------------
# Claude Code hook installation (setup action)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_installs_stop_hook(mailbox_env, monkeypatch, tmp_path):
    settings_path = tmp_path / "claude" / "settings.json"
    monkeypatch.setenv("PAL_CLAUDE_SETTINGS_PATH", str(settings_path))

    result = await _run_tool({"action": "setup", "agent": "claude"})
    assert result["metadata"]["changed"] is True

    settings = json.loads(settings_path.read_text())
    entries = settings["hooks"]["Stop"]
    assert len(entries) == 1
    command = entries[0]["hooks"][0]["command"]
    assert "mailbox_hook.py" in command and command.endswith(" claude || exit 0")

    # Idempotent: second run changes nothing.
    result = await _run_tool({"action": "setup"})
    assert result["metadata"]["changed"] is False
    assert len(json.loads(settings_path.read_text())["hooks"]["Stop"]) == 1


@pytest.mark.asyncio
async def test_setup_preserves_existing_settings(mailbox_env, monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    existing = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
    settings_path.write_text(json.dumps(existing))
    monkeypatch.setenv("PAL_CLAUDE_SETTINGS_PATH", str(settings_path))

    await _run_tool({"action": "setup"})

    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "opus"
    commands = [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]
    assert "other" in commands
    assert any("mailbox_hook.py" in command for command in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"hooks": null}',
        '{"hooks": {"Stop": "not a list"}}',
        '{"hooks": {"Stop": ["not a dict"]}}',
    ],
)
async def test_setup_refuses_malformed_settings(mailbox_env, monkeypatch, tmp_path, content):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(content)
    monkeypatch.setenv("PAL_CLAUDE_SETTINGS_PATH", str(settings_path))

    with pytest.raises(ToolExecutionError):
        await _run_tool({"action": "setup"})
    # Never partially rewritten.
    assert settings_path.read_text() == content


@pytest.mark.asyncio
async def test_setup_updates_stale_hook_from_other_checkout(mailbox_env, monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    stale = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /old/repo/scripts/mailbox_hook.py claude || exit 0",
                        }
                    ]
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(stale))
    monkeypatch.setenv("PAL_CLAUDE_SETTINGS_PATH", str(settings_path))

    result = await _run_tool({"action": "setup"})
    assert result["metadata"]["changed"] is True

    settings = json.loads(settings_path.read_text())
    commands = [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]
    assert len(commands) == 1
    assert "/old/repo" not in commands[0]
    assert "mailbox_hook.py" in commands[0]


@pytest.mark.asyncio
async def test_setup_upgrades_old_style_hook_command(mailbox_env, monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    # An install from before the "|| exit 0" suffix: same interpreter and script,
    # only the suffix is missing.
    new_command = MailboxTool()._claude_hook_command()
    old_command = new_command.removesuffix(" || exit 0")
    old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old_command}]}]}}
    settings_path.write_text(json.dumps(old))
    monkeypatch.setenv("PAL_CLAUDE_SETTINGS_PATH", str(settings_path))

    result = await _run_tool({"action": "setup"})
    assert result["metadata"]["changed"] is True

    settings = json.loads(settings_path.read_text())
    commands = [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]
    assert commands == [new_command]
