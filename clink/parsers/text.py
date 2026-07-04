"""Passthrough parser for CLIs that emit plain text."""

from __future__ import annotations

from .base import BaseParser, ParsedCLIResponse, ParserError


class TextParser(BaseParser):
    """Return CLI stdout verbatim, falling back to stderr when stdout is empty."""

    name = "text"

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        content = stdout.strip()
        if not content:
            content = stderr.strip()
        if not content:
            raise ParserError("CLI produced no output on stdout or stderr")

        metadata: dict[str, object] = {}
        if stderr.strip() and stdout.strip():
            metadata["stderr"] = stderr.strip()
        return ParsedCLIResponse(content=content, metadata=metadata)
