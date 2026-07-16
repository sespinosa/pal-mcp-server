"""Tests for content-block helpers and clink's rich-content consumers."""

import base64
import json
from pathlib import Path

import pytest
from mcp.types import AudioContent, ImageContent, ResourceLink, TextContent

from clink.agents import AgentOutput
from clink.parsers.base import ParsedCLIResponse
from tools.clink import MAX_ARTIFACTS, MAX_RESPONSE_CHARS, CLinkTool, extract_artifacts
from tools.shared.content_blocks import MAX_INLINE_BYTES, file_block

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestFileBlock:
    def test_small_image_is_inlined(self, tmp_path):
        path = tmp_path / "chart.png"
        path.write_bytes(PNG_1PX)

        block = file_block(path)

        assert isinstance(block, ImageContent)
        assert block.mimeType == "image/png"
        assert base64.b64decode(block.data) == PNG_1PX

    def test_small_audio_is_inlined(self, tmp_path):
        path = tmp_path / "clip.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00audio")

        block = file_block(path)

        assert isinstance(block, AudioContent)
        assert block.mimeType == "audio/mpeg"

    def test_oversized_image_becomes_resource_link(self, tmp_path):
        path = tmp_path / "huge.png"
        path.write_bytes(b"\x00" * (MAX_INLINE_BYTES + 1))

        block = file_block(path)

        assert isinstance(block, ResourceLink)
        assert block.size == MAX_INLINE_BYTES + 1

    def test_text_file_becomes_resource_link(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_text("hello")

        block = file_block(path)

        assert isinstance(block, ResourceLink)
        assert str(block.uri).startswith("file://")
        assert block.name == "log.txt"
        assert block.mimeType == "text/plain"


class TestExtractArtifacts:
    def test_valid_file_is_attached_and_tag_stripped(self, tmp_path):
        path = tmp_path / "result.png"
        path.write_bytes(PNG_1PX)
        content = f"Chart written.\n<ARTIFACT>{path}</ARTIFACT>\nDone."

        cleaned, blocks = extract_artifacts(content)

        assert "<ARTIFACT>" not in cleaned
        assert str(path) in cleaned
        assert len(blocks) == 1
        assert isinstance(blocks[0], ImageContent)

    def test_invalid_paths_degrade_to_plain_text(self):
        content = (
            "<ARTIFACT>/etc/passwd</ARTIFACT> "
            "<ARTIFACT>relative/path.png</ARTIFACT> "
            "<ARTIFACT>/tmp/definitely-missing-file-xyz.png</ARTIFACT>"
        )

        cleaned, blocks = extract_artifacts(content)

        assert blocks == []
        assert "<ARTIFACT>" not in cleaned
        assert "/etc/passwd" in cleaned

    def test_artifact_count_is_capped(self, tmp_path):
        tags = []
        for i in range(MAX_ARTIFACTS + 2):
            path = tmp_path / f"file{i}.txt"
            path.write_text("x")
            tags.append(f"<ARTIFACT>{path}</ARTIFACT>")

        _, blocks = extract_artifacts(" ".join(tags))

        assert len(blocks) == MAX_ARTIFACTS


def _agent_output(content: str) -> AgentOutput:
    return AgentOutput(
        parsed=ParsedCLIResponse(content=content, metadata={"model_used": "gemini-2.5-pro"}),
        sanitized_command=["gemini"],
        returncode=0,
        stdout="{}",
        stderr="",
        duration_seconds=0.1,
        parser_name="gemini_json",
        output_file_content=None,
    )


def _patch_agent(monkeypatch, content: str) -> None:
    class DummyAgent:
        async def run(self, **kwargs):
            return _agent_output(content)

    monkeypatch.setattr("tools.clink.create_agent", lambda client: DummyAgent())


@pytest.mark.asyncio
async def test_clink_execute_attaches_artifact_blocks(monkeypatch, tmp_path):
    tool = CLinkTool()
    path = tmp_path / "diagram.png"
    path.write_bytes(PNG_1PX)
    _patch_agent(monkeypatch, f"Diagram ready. <ARTIFACT>{path}</ARTIFACT>")

    results = await tool.execute({"prompt": "Draw", "cli_name": tool._default_cli_name})

    # The JSON envelope stays the first block; rich blocks follow.
    assert isinstance(results[0], TextContent)
    payload = json.loads(results[0].text)
    assert payload["metadata"]["artifacts_attached"] == 1
    assert len(results) == 2
    assert isinstance(results[1], ImageContent)


@pytest.mark.asyncio
async def test_clink_truncation_attaches_full_output_link(monkeypatch):
    tool = CLinkTool()
    long_text = "B" * (MAX_RESPONSE_CHARS + 1000)
    _patch_agent(monkeypatch, long_text)

    results = await tool.execute({"prompt": "Long", "cli_name": tool._default_cli_name})

    payload = json.loads(results[0].text)
    metadata = payload["metadata"]
    full_file = metadata.get("output_full_file")
    assert full_file, "full output should be persisted on truncation"
    try:
        assert Path(full_file).read_text(encoding="utf-8") == long_text
        assert str(full_file) in payload["content"]
        assert len(results) == 2
        assert isinstance(results[1], ResourceLink)
    finally:
        Path(full_file).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_clink_summary_path_attaches_no_blocks(monkeypatch):
    tool = CLinkTool()
    long_text = "A" * (MAX_RESPONSE_CHARS + 500) + "<SUMMARY>short recap</SUMMARY>"
    _patch_agent(monkeypatch, long_text)

    results = await tool.execute({"prompt": "Long", "cli_name": tool._default_cli_name})

    assert len(results) == 1
    payload = json.loads(results[0].text)
    assert payload["metadata"].get("output_summarized") is True
