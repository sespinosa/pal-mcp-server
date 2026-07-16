"""Tests for asynchronous clink jobs: store, agent API, clink wait=false, jobs tool."""

import json
import os
from pathlib import Path

import pytest

from clink.agents import AgentOutput
from clink.agents.dispatch import DispatchAgent
from clink.constants import DISPATCH_RUNNER
from clink.models import DispatchConfig, ResolvedCLIClient, ResolvedCLIRole
from clink.parsers.base import ParsedCLIResponse
from tools.clink import CLinkTool
from tools.jobs import JobsTool
from tools.shared.exceptions import ToolExecutionError
from utils import jobs as job_store

PROMPT_PATH = Path("systemprompts/clink/default.txt").resolve()


def _make_dispatch_client() -> ResolvedCLIClient:
    return ResolvedCLIClient(
        name="cloudcli",
        executable=["cloudcli"],
        internal_args=[],
        config_args=["submit"],
        env={},
        timeout_seconds=60,
        parser="text",
        runner=DISPATCH_RUNNER,
        roles={"default": ResolvedCLIRole(name="default", prompt_path=PROMPT_PATH, role_args=[])},
        output_to_file=None,
        working_dir=None,
        dispatch=DispatchConfig(
            handle_pattern=r"task_id:\s*(\S+)",
            poll_args=["status", "{handle}"],
            done_pattern=r"state:\s*completed",
            failed_pattern=r"state:\s*failed",
            collect_args=["logs", "{handle}"],
        ),
    )


class StubDispatchAgent(DispatchAgent):
    """DispatchAgent with canned poll states and collect output (no subprocesses)."""

    def __init__(self, client, *, states=None, collect_output="collected result"):
        super().__init__(client)
        self.states = list(states or [])
        self.collect_output = collect_output
        self.collect_calls = 0

    async def check(self, handle):
        state = self.states.pop(0)
        return state, f"state: {state}", ""

    async def collect(self, handle):
        self.collect_calls += 1
        return AgentOutput(
            parsed=ParsedCLIResponse(content=self.collect_output, metadata={"model_used": "cloud-model"}),
            sanitized_command=["cloudcli", "logs", handle],
            returncode=0,
            stdout=self.collect_output,
            stderr="",
            duration_seconds=0.1,
            parser_name="text",
            output_file_content=None,
        )


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(job_store, "jobs_dir", lambda: tmp_path / "jobs")
    return job_store


class FakeRegistry:
    def __init__(self, client):
        self._client = client

    def get_client(self, name):
        if self._client is None or name != self._client.name:
            raise KeyError(f"CLI '{name}' is not configured")
        return self._client


def _jobs_tool(monkeypatch, client, agent) -> JobsTool:
    monkeypatch.setattr("tools.jobs.get_registry", lambda: FakeRegistry(client))
    monkeypatch.setattr("tools.jobs.create_agent", lambda c: agent)
    return JobsTool()


def _payload(results):
    return json.loads(results[0].text)


class TestJobStore:
    def test_create_and_get_roundtrip(self, store):
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        loaded = store.get_job(record.job_id)

        assert loaded is not None
        assert loaded.handle == "abc-123"
        assert loaded.state == store.STATE_RUNNING
        assert loaded.kind == "remote"

    def test_expired_record_is_deleted_on_get(self, store):
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")
        record.created_at -= store.JOB_TTL_SECONDS + 10
        store.save_job(record)

        assert store.get_job(record.job_id) is None
        assert not (store.jobs_dir() / f"{record.job_id}.json").exists()

    def test_sweep_removes_stale_and_corrupt(self, store):
        fresh = store.create_job(cli_name="cloudcli", role="default", handle="h1")
        stale = store.create_job(cli_name="cloudcli", role="default", handle="h2")
        stale.created_at -= store.JOB_TTL_SECONDS + 10
        store.save_job(stale)
        (store.jobs_dir() / "corrupt.json").write_text("{not json", encoding="utf-8")

        records = store.list_jobs()

        assert [r.job_id for r in records] == [fresh.job_id]

    def test_get_rejects_path_like_ids(self, store):
        store.create_job(cli_name="cloudcli", role="default", handle="h1")

        assert store.get_job("../evil") is None

    def test_save_uses_unique_temp_and_sweeps_orphans(self, store):
        # Atomic save must not leave the record's final file as a temp, and stale
        # orphaned temp files (from a crash mid-write) get reaped by the sweep.
        record = store.create_job(cli_name="cloudcli", role="default", handle="h1")
        assert (store.jobs_dir() / f"{record.job_id}.json").exists()
        assert not list(store.jobs_dir().glob("*.tmp"))

        orphan = store.jobs_dir() / f"{record.job_id}.orphan.tmp"
        orphan.write_text("partial", encoding="utf-8")
        os.utime(orphan, (0, 0))  # far in the past → older than TTL
        store.sweep_stale()
        assert not orphan.exists()
        assert store.get_job(record.job_id) is not None


class TestDispatchAgentAsyncApi:
    @pytest.mark.asyncio
    async def test_check_states(self, monkeypatch):
        import asyncio
        import shutil

        client = _make_dispatch_client()
        agent = DispatchAgent(client)
        outputs = [b"state: running", b"state: completed", b"state: failed"]

        class Proc:
            def __init__(self, out):
                self._out = out
                self.returncode = 0

            async def communicate(self, _input):
                return self._out, b""

        queue = list(outputs)

        async def fake_exec(*args, **kwargs):
            return Proc(queue.pop(0))

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

        assert (await agent.check("abc"))[0] == "running"
        assert (await agent.check("abc"))[0] == "done"
        assert (await agent.check("abc"))[0] == "failed"

    @pytest.mark.asyncio
    async def test_collect_runs_collect_phase(self, monkeypatch):
        import asyncio
        import shutil

        client = _make_dispatch_client()
        agent = DispatchAgent(client)
        commands = []

        class Proc:
            returncode = 0

            async def communicate(self, _input):
                return b"final result", b""

        async def fake_exec(*args, **kwargs):
            commands.append(list(args))
            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

        result = await agent.collect("abc-123")

        assert result.parsed.content == "final result"
        assert commands[0] == ["/usr/bin/cloudcli", "logs", "abc-123"]
        assert result.parsed.metadata["dispatch_handle"] == "abc-123"


class TestCLinkWaitFalse:
    @pytest.mark.asyncio
    async def test_dispatches_and_creates_job(self, monkeypatch, store):
        tool = CLinkTool()
        client = _make_dispatch_client()
        monkeypatch.setattr(tool, "_registry", FakeRegistry(client))

        class Agent(DispatchAgent):
            async def dispatch_only(self, *, role, prompt, system_prompt=None):
                return "abc-123", ["cloudcli", "submit"]

        monkeypatch.setattr("tools.clink.create_agent", lambda c: Agent(c))

        results = await tool.execute({"prompt": "go", "cli_name": "cloudcli", "wait": False})

        assert len(results) == 1
        payload = _payload(results)
        assert payload["status"] == "success"
        metadata = payload["metadata"]
        assert metadata["dispatch_handle"] == "abc-123"
        assert metadata["state"] == "running"
        record = store.get_job(metadata["job_id"])
        assert record is not None
        assert record.cli_name == "cloudcli"

    @pytest.mark.asyncio
    async def test_rejected_for_blocking_client(self):
        tool = CLinkTool()

        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute({"prompt": "go", "cli_name": tool._default_cli_name, "wait": False})

        assert "wait=false requires" in json.loads(str(exc_info.value))["content"]

    @pytest.mark.asyncio
    async def test_wait_true_is_default_blocking_path(self, monkeypatch):
        tool = CLinkTool()

        class DummyAgent:
            async def run(self, **kwargs):
                return AgentOutput(
                    parsed=ParsedCLIResponse(content="blocking result", metadata={}),
                    sanitized_command=["gemini"],
                    returncode=0,
                    stdout="{}",
                    stderr="",
                    duration_seconds=0.1,
                    parser_name="gemini_json",
                    output_file_content=None,
                )

        monkeypatch.setattr("tools.clink.create_agent", lambda c: DummyAgent())

        results = await tool.execute({"prompt": "go", "cli_name": tool._default_cli_name})

        assert "blocking result" in _payload(results)["content"]


class TestJobsTool:
    @pytest.mark.asyncio
    async def test_list_empty(self, monkeypatch, store):
        tool = _jobs_tool(monkeypatch, _make_dispatch_client(), None)

        payload = _payload(await tool.execute({"action": "list"}))

        assert payload["status"] == "success"
        assert payload["metadata"]["jobs"] == []

    @pytest.mark.asyncio
    async def test_status_polls_once_and_persists(self, monkeypatch, store):
        client = _make_dispatch_client()
        agent = StubDispatchAgent(client, states=["running", "done"])
        tool = _jobs_tool(monkeypatch, client, agent)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        payload = _payload(await tool.execute({"action": "status", "job_id": record.job_id}))
        assert payload["metadata"]["state"] == "running"
        assert store.get_job(record.job_id).poll_attempts == 1

        payload = _payload(await tool.execute({"action": "status", "job_id": record.job_id}))
        assert payload["metadata"]["state"] == "done"
        assert "collect" in payload["content"]

        # Terminal states answer from the record without polling again.
        payload = _payload(await tool.execute({"action": "status", "job_id": record.job_id}))
        assert payload["metadata"]["state"] == "done"
        assert agent.states == []

    @pytest.mark.asyncio
    async def test_status_failed_records_error(self, monkeypatch, store):
        client = _make_dispatch_client()
        agent = StubDispatchAgent(client, states=["failed"])
        tool = _jobs_tool(monkeypatch, client, agent)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        payload = _payload(await tool.execute({"action": "status", "job_id": record.job_id}))

        assert payload["metadata"]["state"] == "failed"
        assert store.get_job(record.job_id).error

    @pytest.mark.asyncio
    async def test_collect_done_job_and_recollect_from_record(self, monkeypatch, store):
        client = _make_dispatch_client()
        agent = StubDispatchAgent(client, states=["done"])
        tool = _jobs_tool(monkeypatch, client, agent)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        payload = _payload(await tool.execute({"action": "collect", "job_id": record.job_id}))

        assert payload["content"] == "collected result"
        assert payload["metadata"]["model_used"] == "cloud-model"
        assert store.get_job(record.job_id).state == store.STATE_COLLECTED
        assert agent.collect_calls == 1

        # Idempotent re-collect: served from the stored record, no second collect phase.
        payload = _payload(await tool.execute({"action": "collect", "job_id": record.job_id}))
        assert payload["content"] == "collected result"
        assert agent.collect_calls == 1

    @pytest.mark.asyncio
    async def test_collect_running_job_reports_not_done(self, monkeypatch, store):
        client = _make_dispatch_client()
        agent = StubDispatchAgent(client, states=["running"])
        tool = _jobs_tool(monkeypatch, client, agent)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        payload = _payload(await tool.execute({"action": "collect", "job_id": record.job_id}))

        assert payload["status"] == "success"
        assert payload["metadata"]["state"] == "running"
        assert "Not done yet" in payload["content"]

    @pytest.mark.asyncio
    async def test_collect_failed_job_raises(self, monkeypatch, store):
        client = _make_dispatch_client()
        agent = StubDispatchAgent(client, states=["failed"])
        tool = _jobs_tool(monkeypatch, client, agent)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        with pytest.raises(ToolExecutionError):
            await tool.execute({"action": "collect", "job_id": record.job_id})

    @pytest.mark.asyncio
    async def test_unknown_job_id(self, monkeypatch, store):
        tool = _jobs_tool(monkeypatch, _make_dispatch_client(), None)

        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute({"action": "status", "job_id": "deadbeef1234"})

        assert "Unknown or expired" in json.loads(str(exc_info.value))["content"]

    @pytest.mark.asyncio
    async def test_client_no_longer_configured(self, monkeypatch, store):
        tool = _jobs_tool(monkeypatch, None, None)
        record = store.create_job(cli_name="cloudcli", role="default", handle="abc-123")

        with pytest.raises(ToolExecutionError) as exc_info:
            await tool.execute({"action": "status", "job_id": record.job_id})

        assert "no longer configured" in json.loads(str(exc_info.value))["content"]
