import pytest

from clink.parsers.base import ParserError
from clink.parsers.codex import CodexJSONLParser
from clink.parsers.text import TextParser


def test_codex_parser_success():
    parser = CodexJSONLParser()
    stdout = """
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello"}}
{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}
"""
    parsed = parser.parse(stdout=stdout, stderr="")
    assert parsed.content == "Hello"
    assert parsed.metadata["usage"]["output_tokens"] == 5


def test_codex_parser_requires_agent_message():
    parser = CodexJSONLParser()
    stdout = '{"type":"turn.completed"}'
    with pytest.raises(ParserError):
        parser.parse(stdout=stdout, stderr="")


def test_text_parser_returns_stdout_verbatim():
    parsed = TextParser().parse(stdout="plain result\n", stderr="warning: noise")
    assert parsed.content == "plain result"
    assert parsed.metadata["stderr"] == "warning: noise"


def test_text_parser_falls_back_to_stderr():
    parsed = TextParser().parse(stdout="", stderr="only stderr output")
    assert parsed.content == "only stderr output"
    assert "stderr" not in parsed.metadata


def test_text_parser_rejects_empty_output():
    with pytest.raises(ParserError):
        TextParser().parse(stdout="  ", stderr="")
