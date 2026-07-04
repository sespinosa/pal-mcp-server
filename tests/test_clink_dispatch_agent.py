"""Tests for the dispatch/poll clink agent and its configuration."""

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from clink.agents import create_agent
from clink.agents.base import CLIAgentError
from clink.agents.dispatch import DispatchAgent
from clink.constants import DISPATCH_RUNNER
from clink.models import DispatchConfig, ResolvedCLIClient, ResolvedCLIRole
from clink.registry import ClinkRegistry, RegistryLoadError

PROMPT_PATH = Path("systemprompts/clink/default.txt").resolve()


class DummyProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, _input):
        return self._stdout, self._stderr


def _make_client(dispatch: DispatchConfig, timeout_seconds: int = 60) -> ResolvedCLIClient:
    return ResolvedCLIClient(
        name="cloudcli",
        executable=["cloudcli"],
        internal_args=[],
        config_args=["submit"],
        env={},
        timeout_seconds=timeout_seconds,
        parser="text",
        runner=DISPATCH_RUNNER,
        roles={"default": ResolvedCLIRole(name="default", prompt_path=PROMPT_PATH, role_args=[])},
        output_to_file=None,
        working_dir=None,
        dispatch=dispatch,
    )


def _install_process_sequence(monkeypatch, processes):
    """Feed successive subprocess calls from a list, recording each command."""
    commands: list[list[str]] = []
    queue = list(processes)

    async def fake_create_subprocess_exec(*args, **_kwargs):
        commands.append(list(args))
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    return commands


async def _run(agent, client):
    return await agent.run(role=client.get_role("default"), prompt="do something", files=[], images=[])


@pytest.mark.asyncio
async def test_fire_and_forget_returns_dispatch_ack(monkeypatch):
    dispatch = DispatchConfig(handle_pattern=r"task_id:\s*(\S+)")
    client = _make_client(dispatch)
    agent = create_agent(client)
    assert isinstance(agent, DispatchAgent)

    _install_process_sequence(monkeypatch, [DummyProcess(stdout=b"Dispatched.\ntask_id: abc-123\nurl: https://x")])

    result = await _run(agent, client)

    assert result.parsed.metadata["dispatch_handle"] == "abc-123"
    assert "task_id: abc-123" in result.parsed.content
    assert "poll_attempts" not in result.parsed.metadata


@pytest.mark.asyncio
async def test_poll_until_done_then_collect(monkeypatch):
    dispatch = DispatchConfig(
        handle_pattern=r"task_id:\s*(\S+)",
        poll_args=["status", "{handle}"],
        poll_interval_seconds=1,
        done_pattern=r"state:\s*completed",
        failed_pattern=r"state:\s*failed",
        collect_args=["logs", "{handle}"],
    )
    client = _make_client(dispatch)
    agent = create_agent(client)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    commands = _install_process_sequence(
        monkeypatch,
        [
            DummyProcess(stdout=b"task_id: abc-123"),
            DummyProcess(stdout=b"state: running"),
            DummyProcess(stdout=b"state: completed"),
            DummyProcess(stdout=b"final answer from the cloud"),
        ],
    )

    result = await _run(agent, client)

    assert result.parsed.content == "final answer from the cloud"
    assert result.parsed.metadata["dispatch_handle"] == "abc-123"
    assert result.parsed.metadata["poll_attempts"] == 2
    assert len(sleeps) == 1
    # Poll and collect commands substitute the handle and skip config args.
    assert commands[1] == ["/usr/bin/cloudcli", "status", "abc-123"]
    assert commands[3] == ["/usr/bin/cloudcli", "logs", "abc-123"]


@pytest.mark.asyncio
async def test_poll_failure_pattern_raises(monkeypatch):
    dispatch = DispatchConfig(
        handle_pattern=r"task_id:\s*(\S+)",
        poll_args=["status", "{handle}"],
        done_pattern=r"state:\s*completed",
        failed_pattern=r"state:\s*failed",
    )
    client = _make_client(dispatch)
    agent = create_agent(client)

    _install_process_sequence(
        monkeypatch,
        [
            DummyProcess(stdout=b"task_id: abc-123"),
            DummyProcess(stdout=b"state: failed"),
        ],
    )

    with pytest.raises(CLIAgentError, match="failed"):
        await _run(agent, client)


@pytest.mark.asyncio
async def test_last_poll_output_is_result_without_collect(monkeypatch):
    dispatch = DispatchConfig(
        handle_pattern=r"task_id:\s*(\S+)",
        poll_args=["status", "{handle}"],
        done_pattern=r"state:\s*completed",
    )
    client = _make_client(dispatch)
    agent = create_agent(client)

    _install_process_sequence(
        monkeypatch,
        [
            DummyProcess(stdout=b"task_id: abc-123"),
            DummyProcess(stdout=b"state: completed\nresult: 42"),
        ],
    )

    result = await _run(agent, client)
    assert "result: 42" in result.parsed.content
    assert result.parsed.metadata["poll_attempts"] == 1


@pytest.mark.asyncio
async def test_missing_handle_raises(monkeypatch):
    dispatch = DispatchConfig(handle_pattern=r"task_id:\s*(\S+)")
    client = _make_client(dispatch)
    agent = create_agent(client)

    _install_process_sequence(monkeypatch, [DummyProcess(stdout=b"submitted, no id here")])

    with pytest.raises(CLIAgentError, match="task handle"):
        await _run(agent, client)


@pytest.mark.asyncio
async def test_dispatch_nonzero_exit_raises(monkeypatch):
    dispatch = DispatchConfig(handle_pattern=r"task_id:\s*(\S+)")
    client = _make_client(dispatch)
    agent = create_agent(client)

    _install_process_sequence(monkeypatch, [DummyProcess(stderr=b"quota exceeded", returncode=2)])

    with pytest.raises(CLIAgentError, match="dispatch failed"):
        await _run(agent, client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        b"task_id: --dangerous-flag",  # would be parsed as a flag by the poll CLI
        b"task_id: a;b|c",  # hostile charset
        b"submitted",  # group does not participate in the match -> None handle
    ],
)
async def test_unusable_handles_rejected(monkeypatch, output):
    dispatch = DispatchConfig(handle_pattern=r"(?:task_id:\s*(\S+))|submitted")
    client = _make_client(dispatch)
    agent = create_agent(client)

    _install_process_sequence(monkeypatch, [DummyProcess(stdout=output)])

    with pytest.raises(CLIAgentError, match="handle"):
        await _run(agent, client)


@pytest.mark.asyncio
async def test_timeout_while_polling_reports_handle(monkeypatch):
    dispatch = DispatchConfig(
        handle_pattern=r"task_id:\s*(\S+)",
        poll_args=["status", "{handle}"],
        done_pattern=r"state:\s*completed",
    )
    client = _make_client(dispatch, timeout_seconds=1)
    agent = create_agent(client)

    # Each monotonic() call advances 0.6s, so the 1s budget expires before the first poll.
    clock = {"now": 0.0}

    def fake_monotonic():
        clock["now"] += 0.6
        return clock["now"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    _install_process_sequence(monkeypatch, [DummyProcess(stdout=b"task_id: abc-123")])

    with pytest.raises(CLIAgentError, match="abc-123.*may still be running"):
        await _run(agent, client)


@pytest.mark.asyncio
async def test_prompt_placeholder_moves_prompt_to_argv(monkeypatch):
    dispatch = DispatchConfig(handle_pattern=r"task_id:\s*(\S+)")
    client = _make_client(dispatch)
    client.config_args = ["submit", "--task", "{prompt}"]
    agent = create_agent(client)

    captured_input = {}

    async def fake_communicate(input_bytes):
        captured_input["stdin"] = input_bytes
        return b"task_id: abc-123", b""

    process = DummyProcess()
    process.communicate = fake_communicate
    commands = _install_process_sequence(monkeypatch, [process])

    result = await _run(agent, client)

    assert commands[0][-1] == "do something"
    assert captured_input["stdin"] is None
    # The sanitized command must keep the placeholder, not the full prompt.
    assert "{prompt}" in result.sanitized_command


def test_deadline_exhaustion_raises():
    dispatch = DispatchConfig(handle_pattern=r"x")
    client = _make_client(dispatch)
    agent = DispatchAgent(client)

    with pytest.raises(CLIAgentError, match="timed out"):
        agent._remaining(time.monotonic() - 1)


def test_poll_args_require_done_pattern():
    with pytest.raises(ValidationError, match="done_pattern"):
        DispatchConfig(handle_pattern=r"x", poll_args=["status", "{handle}"])


def test_poll_only_fields_rejected_without_poll_args():
    with pytest.raises(ValidationError, match="require dispatch.poll_args"):
        DispatchConfig(handle_pattern=r"x", collect_args=["result", "{handle}"])
    with pytest.raises(ValidationError, match="require dispatch.poll_args"):
        DispatchConfig(handle_pattern=r"x", done_pattern=r"done")


def test_client_named_dispatch_without_config_uses_base_agent():
    from clink.agents.base import BaseCLIAgent

    client = _make_client(DispatchConfig(handle_pattern=r"x"))
    client.name = "dispatch"
    client.dispatch = None
    client.runner = None

    agent = create_agent(client)
    assert type(agent) is BaseCLIAgent


def test_registry_loads_custom_dispatch_client(monkeypatch, tmp_path):
    config = {
        "name": "cloudcli",
        "command": "cloudcli",
        "parser": "text",
        "additional_args": ["submit"],
        "dispatch": {
            "handle_pattern": r"task_id:\s*(\S+)",
            "poll_args": ["status", "{handle}"],
            "done_pattern": r"completed",
        },
    }
    config_path = tmp_path / "cloudcli.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    registry = ClinkRegistry()
    client = registry.get_client("cloudcli")

    assert client.runner == DISPATCH_RUNNER
    assert client.parser == "text"
    assert client.dispatch is not None
    assert client.dispatch.poll_interval_seconds == 15
    # Unknown CLIs fall back to the generic default role prompt.
    assert client.get_role("default").prompt_path.name == "default.txt"


def test_registry_rejects_custom_client_without_parser(monkeypatch, tmp_path):
    config_path = tmp_path / "mystery.json"
    config_path.write_text(json.dumps({"name": "mystery", "command": "mystery"}))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    with pytest.raises(RegistryLoadError, match="parser"):
        ClinkRegistry()


def test_registry_rejects_dispatch_with_output_to_file(monkeypatch, tmp_path):
    config = {
        "name": "cloudcli",
        "command": "cloudcli",
        "parser": "text",
        "output_to_file": {"flag_template": "--output {path}"},
        "dispatch": {"handle_pattern": r"task_id:\s*(\S+)"},
    }
    config_path = tmp_path / "cloudcli.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("CLI_CLIENTS_CONFIG_PATH", str(config_path))

    with pytest.raises(RegistryLoadError, match="output_to_file"):
        ClinkRegistry()
