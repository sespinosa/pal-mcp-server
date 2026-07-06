"""Mailbox tool - peer-to-peer messaging between agent sessions.

PAL's clink tool is hub-and-spoke: spawned CLIs report back to the parent, but they
cannot talk to each other while running. This tool adds a shared, file-based mailbox
(see utils/mailbox.py) so any session with PAL configured (the parent or any spawned
CLI) can send messages to any other by agent id.

Delivery:
- Every agent can poll explicitly with the ``check`` action.
- For hands-free delivery, the ``setup`` action registers a Claude Code ``Stop`` hook
  (scripts/mailbox_hook.py) that injects pending messages into a session's context
  whenever it finishes a turn. The hook only activates in sessions that set
  ``PAL_AGENT_ID``, so interactive human sessions are unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from tools.models import ToolModelCategory, ToolOutput
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool
from tools.shared.exceptions import ToolExecutionError
from utils.mailbox import (
    MailboxError,
    current_agent_id,
    drain_messages,
    format_messages,
    list_stack,
    mailbox_root,
    send_message,
)

logger = logging.getLogger(__name__)

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mailbox_hook.py"


class MailboxTool(BaseTool):
    """Send, receive, and configure delivery of inter-agent messages."""

    def get_name(self) -> str:
        return "mailbox"

    def get_description(self) -> str:
        return (
            "Message other live agent sessions by agent id (clink-spawned workers, sibling sessions, or "
            "your parent orchestrator) instead of waiting for their final output. Send follow-up "
            "instructions to running workers, check messages addressed to you, list the live agents in "
            "your stack, or install automatic turn-end delivery hooks."
        )

    def get_annotations(self) -> dict[str, Any] | None:
        # 'send' and 'setup' write outside the conversation (spool files / settings).
        return {"readOnlyHint": False}

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send", "check", "list", "setup"],
                    "description": (
                        "'send' a message to another agent, 'check' your own mailbox (messages are "
                        "consumed on read), 'list' the live agents in your own stack (your workers and "
                        "your parent), or 'setup' automatic turn-end delivery for a coding agent CLI. "
                        "Your own agent id comes from the PAL_AGENT_ID environment variable (default: "
                        "'parent'). Address workers by the exact id clink reported when spawning them."
                    ),
                },
                "to": {
                    "type": "string",
                    "description": "Recipient agent id (required for 'send').",
                },
                "message": {
                    "type": "string",
                    "description": "Message text (required for 'send').",
                },
                "agent": {
                    "type": "string",
                    "enum": ["claude"],
                    "description": "Which coding agent CLI to install delivery hooks for (for 'setup'). Default: claude.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.FAST_RESPONSE

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        try:
            if action == "send":
                result = self._send(arguments)
            elif action == "check":
                result = self._check()
            elif action == "list":
                result = self._list()
            elif action == "setup":
                result = self._setup(arguments)
            else:
                raise MailboxError(f"Unknown action {action!r}. Use 'send', 'check', 'list' or 'setup'.")
        except MailboxError as exc:
            error_output = ToolOutput(status="error", content=str(exc), content_type="text")
            raise ToolExecutionError(error_output.model_dump_json()) from exc

        return [TextContent(type="text", text=result.model_dump_json())]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _send(self, arguments: dict[str, Any]) -> ToolOutput:
        to = arguments.get("to") or ""
        text = arguments.get("message") or ""
        message = send_message(sender=current_agent_id(), recipient=to, text=text)
        return ToolOutput(
            status="success",
            content=(
                f"Message queued for '{message.recipient}' (from '{message.sender}'). "
                "It is delivered when the recipient checks its mailbox or finishes a turn "
                "(if delivery hooks are set up)."
            ),
            content_type="text",
            metadata={"tool_name": self.name, "to": message.recipient, "from": message.sender},
        )

    def _check(self) -> ToolOutput:
        agent_id = current_agent_id()
        messages = drain_messages(agent_id)
        if not messages:
            content = f"No pending messages for '{agent_id}'."
        else:
            content = f"{len(messages)} message(s) for '{agent_id}':\n\n{format_messages(messages)}"
        return ToolOutput(
            status="success",
            content=content,
            content_type="text",
            metadata={"tool_name": self.name, "agent_id": agent_id, "message_count": len(messages)},
        )

    def _list(self) -> ToolOutput:
        agent_id = current_agent_id()
        stack = list_stack(agent_id)
        lines = [f"Agent id: '{agent_id}'"]
        if stack["parent_id"]:
            lines.append(f"Parent: '{stack['parent_id']}'")
        if stack["workers"]:
            lines.append(f"{len(stack['workers'])} live worker(s) in your stack:")
            for worker in stack["workers"]:
                lines.append(
                    f"- '{worker['agent_id']}' ({worker.get('client', '?')}, role {worker.get('role', '?')}, "
                    f"since {worker.get('started_at', '?')})"
                )
        else:
            lines.append("No live workers in your stack.")
        return ToolOutput(
            status="success",
            content="\n".join(lines),
            content_type="text",
            metadata={
                "tool_name": self.name,
                "agent_id": agent_id,
                "parent_id": stack["parent_id"],
                "worker_count": len(stack["workers"]),
            },
        )

    def _setup(self, arguments: dict[str, Any]) -> ToolOutput:
        agent = arguments.get("agent") or "claude"
        if agent != "claude":
            raise MailboxError(f"No delivery hook support for agent {agent!r} yet. Supported: claude.")
        settings_path, changed = self._install_claude_stop_hook()

        hook_command = self._claude_hook_command()
        lines = [
            f"Claude Code Stop hook {'installed in' if changed else 'already present in'} `{settings_path}`:",
            f"- command: `{hook_command}`",
            f"- mailbox root: `{mailbox_root()}`",
            "",
            "The hook only activates in sessions that set PAL_AGENT_ID. To give a clink-spawned Claude an "
            'address, add e.g. `"env": {"PAL_AGENT_ID": "claude-worker"}` to its conf/cli_clients JSON. '
            "Restart running sessions to pick up the hook. To uninstall, remove that entry from the "
            "settings file.",
        ]
        return ToolOutput(
            status="success",
            content="\n".join(lines),
            content_type="text",
            metadata={
                "tool_name": self.name,
                "settings_path": str(settings_path),
                "changed": changed,
            },
        )

    # ------------------------------------------------------------------
    # Claude Code hook installation
    # ------------------------------------------------------------------

    def _claude_settings_path(self) -> Path:
        # Deliberately raw process env (not utils.env.get_env): these are per-process
        # runtime knobs, and .env override semantics must never redirect a test or a
        # sandboxed run back to the real user settings.
        override = os.environ.get("PAL_CLAUDE_SETTINGS_PATH")
        if override:
            return Path(override).expanduser()
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
        return base / "settings.json"

    def _claude_hook_command(self) -> str:
        # "|| exit 0" so a moved or deleted install degrades to a silent no-op
        # instead of exit 2, which would block every session's stop.
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(HOOK_SCRIPT))} claude || exit 0"

    @staticmethod
    def _is_mailbox_hook_command(command: Any) -> bool:
        if not isinstance(command, str) or "mailbox_hook.py" not in command:
            return False
        # Match old-style installs too so setup upgrades them in place.
        return command.endswith(" claude") or command.endswith(" claude || exit 0")

    def _install_claude_stop_hook(self) -> tuple[Path, bool]:
        """Add the mailbox Stop hook to Claude Code settings. Returns (path, changed)."""
        # resolve() so an atomic replace rewrites the target of a symlinked settings
        # file instead of replacing the symlink itself.
        settings_path = self._claude_settings_path()
        if settings_path.exists():
            settings_path = settings_path.resolve()

        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MailboxError(
                    f"Could not read Claude settings at {settings_path}: {exc}. Fix or remove the file and retry."
                )
            if not isinstance(settings, dict):
                raise MailboxError(f"Claude settings at {settings_path} is not a JSON object; refusing to modify it.")

        hooks_config = settings.setdefault("hooks", {})
        if not isinstance(hooks_config, dict):
            raise MailboxError(
                f"Claude settings at {settings_path} has a non-object 'hooks' key; refusing to modify it."
            )
        stop_hooks = hooks_config.setdefault("Stop", [])
        if not isinstance(stop_hooks, list) or not all(isinstance(entry, dict) for entry in stop_hooks):
            raise MailboxError(
                f"Claude settings at {settings_path} has an unexpected 'hooks.Stop' structure; refusing to modify it."
            )

        hook_command = self._claude_hook_command()
        changed = False
        found = False
        for entry in stop_hooks:
            entry_hooks = entry.get("hooks")
            if not isinstance(entry_hooks, list):
                continue
            for hook in entry_hooks:
                if not isinstance(hook, dict):
                    continue
                if hook.get("command") == hook_command:
                    found = True
                elif self._is_mailbox_hook_command(hook.get("command")):
                    # Stale entry from another venv/checkout: point it at this install.
                    hook["command"] = hook_command
                    found = True
                    changed = True

        if not found:
            stop_hooks.append({"hooks": [{"type": "command", "command": hook_command}]})
            changed = True

        if changed:
            self._write_settings_atomically(settings_path, settings)
            logger.info("Installed PAL mailbox Stop hook in %s", settings_path)
        return settings_path, changed

    @staticmethod
    def _write_settings_atomically(settings_path: Path, settings: dict[str, Any]) -> None:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".settings-", dir=settings_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(settings, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, settings_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
