"""Helpers for building non-text MCP content blocks from files.

MCP tool results are lists of ``ContentBlock`` (text, image, audio, resource link,
embedded resource). PAL tools keep their JSON ``ToolOutput`` envelope as the first
block; these helpers build the additional blocks a tool may attach alongside it.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from mcp.types import AudioContent, ContentBlock, ImageContent, ResourceLink
from pydantic import AnyUrl

# Media at or below this size is inlined as base64; anything larger (or non-media)
# becomes a ResourceLink the client fetches on demand.
MAX_INLINE_BYTES = 1_000_000


def file_block(path: str | Path) -> ContentBlock:
    """Map a file to the richest MCP content block that can carry it.

    Small images and audio are inlined (base64 + mimeType); everything else falls
    back to a ``ResourceLink``. The caller is responsible for validating the path
    against file-access security policies before handing it here.
    """
    resolved = Path(path).resolve()
    mime, _ = mimetypes.guess_type(resolved.name)
    size = resolved.stat().st_size

    if mime and size <= MAX_INLINE_BYTES:
        if mime.startswith("image/"):
            data = base64.b64encode(resolved.read_bytes()).decode("ascii")
            return ImageContent(type="image", data=data, mimeType=mime)
        if mime.startswith("audio/"):
            data = base64.b64encode(resolved.read_bytes()).decode("ascii")
            return AudioContent(type="audio", data=data, mimeType=mime)

    return ResourceLink(
        type="resource_link",
        uri=AnyUrl(resolved.as_uri()),
        name=resolved.name,
        mimeType=mime,
        size=size,
    )
