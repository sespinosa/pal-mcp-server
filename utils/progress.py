"""Emit MCP progress notifications during long-running tool operations.

The lowlevel MCP server exposes the active request context through a contextvar,
so any code running inside a tool call can stream progress to the client without
threading a context parameter through every layer. Sending is strictly
best-effort: outside a request context, without a client-supplied progressToken,
or on any transport error this is a no-op — progress must never fail a tool
call. Notifications carry only numbers and a message string (no rich content)
and are only valid while the originating request is still open.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_progress(message: str, progress: float = 0.0, total: float | None = None) -> None:
    """Best-effort MCP ``notifications/progress`` for the active request."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
    except LookupError:
        return

    token = ctx.meta.progressToken if ctx.meta else None
    if token is None:
        return

    try:
        await ctx.session.send_progress_notification(
            progress_token=token,
            progress=progress,
            total=total,
            message=message,
        )
    except Exception:
        logger.debug("Failed to send progress notification", exc_info=True)
