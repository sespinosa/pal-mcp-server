"""Tests for MCP progress notifications (utils/progress.py and its wiring)."""

import pytest
from mcp.server.lowlevel.server import request_ctx

from utils.progress import send_progress


class FakeSession:
    def __init__(self, raise_error: bool = False):
        self.calls = []
        self.raise_error = raise_error

    async def send_progress_notification(self, **kwargs):
        if self.raise_error:
            raise RuntimeError("transport down")
        self.calls.append(kwargs)


class FakeMeta:
    def __init__(self, progress_token):
        self.progressToken = progress_token


class FakeContext:
    def __init__(self, session, meta):
        self.session = session
        self.meta = meta


def _install_ctx(session, meta):
    return request_ctx.set(FakeContext(session, meta))


@pytest.mark.asyncio
async def test_noop_outside_request_context():
    # No contextvar set: must silently do nothing.
    await send_progress("hello")


@pytest.mark.asyncio
async def test_noop_without_progress_token():
    session = FakeSession()
    token = _install_ctx(session, FakeMeta(progress_token=None))
    try:
        await send_progress("hello")
    finally:
        request_ctx.reset(token)
    assert session.calls == []


@pytest.mark.asyncio
async def test_noop_without_meta():
    session = FakeSession()
    token = _install_ctx(session, None)
    try:
        await send_progress("hello")
    finally:
        request_ctx.reset(token)
    assert session.calls == []


@pytest.mark.asyncio
async def test_sends_notification_with_token():
    session = FakeSession()
    token = _install_ctx(session, FakeMeta(progress_token="tok-1"))
    try:
        await send_progress("step 2/5", progress=2, total=5)
    finally:
        request_ctx.reset(token)

    assert session.calls == [{"progress_token": "tok-1", "progress": 2, "total": 5, "message": "step 2/5"}]


@pytest.mark.asyncio
async def test_transport_errors_are_swallowed():
    session = FakeSession(raise_error=True)
    token = _install_ctx(session, FakeMeta(progress_token="tok-1"))
    try:
        await send_progress("hello")  # must not raise
    finally:
        request_ctx.reset(token)


@pytest.mark.asyncio
async def test_dispatch_poll_loop_emits_progress(monkeypatch):
    """The dispatch runner reports each pending poll cycle."""
    import asyncio
    import shutil

    import clink.agents.dispatch as dispatch_module
    from clink.agents import create_agent
    from tests.test_jobs import _make_dispatch_client

    messages = []

    async def fake_send_progress(message, progress=0.0, total=None):
        messages.append(message)

    monkeypatch.setattr(dispatch_module, "send_progress", fake_send_progress)

    class Proc:
        def __init__(self, out):
            self._out = out
            self.returncode = 0

        async def communicate(self, _input):
            return self._out, b""

    queue = [Proc(b"task_id: abc-123"), Proc(b"state: running"), Proc(b"state: completed"), Proc(b"final")]

    async def fake_exec(*args, **kwargs):
        return queue.pop(0)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    client = _make_dispatch_client()
    agent = create_agent(client)
    result = await agent.run(role=client.get_role("default"), prompt="go", files=[], images=[])

    assert result.parsed.content == "final"
    assert any("poll #1" in message for message in messages)
