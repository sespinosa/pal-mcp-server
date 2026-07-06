"""Tests for the mailbox delivery hook script (run as a real subprocess)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils.mailbox import drain_messages, send_message

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mailbox_hook.py"


@pytest.fixture
def mailbox_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAL_MAILBOX_DIR", str(tmp_path / "mailbox"))
    return tmp_path


def _run_hook(env_overrides, stdin_payload='{"hook_event_name": "Stop"}'):
    env = dict(os.environ)
    env.pop("PAL_AGENT_ID", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "claude"],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_hook_delivers_pending_messages(mailbox_env):
    send_message(sender="parent", recipient="worker-1", text="please also update the docs")

    result = _run_hook({"PAL_MAILBOX_DIR": os.environ["PAL_MAILBOX_DIR"], "PAL_AGENT_ID": "worker-1"})

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "please also update the docs" in context
    assert "from parent" in context

    # Delivered messages are claimed.
    assert drain_messages("worker-1") == []


def test_hook_silent_without_agent_id(mailbox_env):
    send_message(sender="parent", recipient="worker-1", text="undelivered")
    result = _run_hook({"PAL_MAILBOX_DIR": os.environ["PAL_MAILBOX_DIR"]})

    assert result.returncode == 0
    assert result.stdout == ""
    # Message stays queued for the real recipient.
    assert len(drain_messages("worker-1")) == 1


def test_hook_silent_with_empty_mailbox(mailbox_env):
    result = _run_hook({"PAL_MAILBOX_DIR": os.environ["PAL_MAILBOX_DIR"], "PAL_AGENT_ID": "worker-1"})
    assert result.returncode == 0
    assert result.stdout == ""


def test_hook_rejects_unknown_agent_kind(mailbox_env):
    env = dict(os.environ)
    env["PAL_AGENT_ID"] = "worker-1"
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "unsupported-cli"],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 1
    assert "unsupported" in result.stderr
