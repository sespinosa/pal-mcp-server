"""clink tool - bridge PAL MCP requests to external AI CLIs."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import ContentBlock, TextContent
from pydantic import BaseModel, Field

from clink import get_registry
from clink.agents import AgentOutput, CLIAgentError, create_agent
from clink.agents.dispatch import DispatchAgent
from clink.models import ResolvedCLIClient, ResolvedCLIRole
from config import TEMPERATURE_BALANCED
from tools.models import ToolModelCategory, ToolOutput
from tools.shared.base_models import COMMON_FIELD_DESCRIPTIONS
from tools.shared.content_blocks import file_block
from tools.shared.exceptions import ToolExecutionError
from tools.simple.base import SchemaBuilder, SimpleTool
from utils import jobs
from utils.file_utils import resolve_and_validate_path
from utils.mailbox import current_agent_id, register_agent, uniquify_agent_id, unregister_agent

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 20_000
SUMMARY_PATTERN = re.compile(r"<SUMMARY>(.*?)</SUMMARY>", re.IGNORECASE | re.DOTALL)
ARTIFACT_PATTERN = re.compile(r"<ARTIFACT>(.*?)</ARTIFACT>", re.IGNORECASE | re.DOTALL)
MAX_ARTIFACTS = 4
WAIT_FIELD_DESCRIPTION = (
    "Set to false to dispatch asynchronously: the call returns a job_id immediately instead of "
    "blocking until the task completes. Only supported for CLIs with a pollable dispatch "
    "configuration. Poll and fetch the result with the 'jobs' tool (actions: status, collect)."
)


def extract_artifacts(content: str) -> tuple[str, list[ContentBlock]]:
    """Turn ``<ARTIFACT>/abs/path</ARTIFACT>`` tags in CLI output into content blocks.

    CLI output is semi-trusted: every path goes through the same security
    validation as user-supplied file paths, and paths that fail it degrade to
    plain text. At most MAX_ARTIFACTS files are attached.
    """
    blocks: list[ContentBlock] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(1).strip()
        if raw and len(blocks) < MAX_ARTIFACTS:
            try:
                path = resolve_and_validate_path(raw)
                if not path.is_file():
                    raise ValueError("not a regular file")
                blocks.append(file_block(path))
            except Exception as exc:
                logger.warning("Clink ignored artifact %r: %s", raw, exc)
        return raw

    return ARTIFACT_PATTERN.sub(_replace, content), blocks


def apply_output_limit(
    client: ResolvedCLIClient,
    content: str,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any], list[ContentBlock]]:
    if len(content) <= MAX_RESPONSE_CHARS:
        return content, metadata, []

    summary = _extract_summary(content)
    if summary:
        summary_text = summary
        if len(summary_text) > MAX_RESPONSE_CHARS:
            logger.debug(
                "Clink summary from %s exceeded %d chars; truncating summary to fit.",
                client.name,
                MAX_RESPONSE_CHARS,
            )
            summary_text = summary_text[:MAX_RESPONSE_CHARS]
        summary_metadata = _prune_metadata(metadata, client, reason="summary")
        summary_metadata.update(
            {
                "output_summarized": True,
                "output_original_length": len(content),
                "output_summary_length": len(summary_text),
                "output_limit": MAX_RESPONSE_CHARS,
            }
        )
        logger.info(
            "Clink compressed %s output via <SUMMARY>: original=%d chars, summary=%d chars",
            client.name,
            len(content),
            len(summary_text),
        )
        return summary_text, summary_metadata, []

    truncated_metadata = _prune_metadata(metadata, client, reason="truncated")
    truncated_metadata.update(
        {
            "output_truncated": True,
            "output_original_length": len(content),
            "output_limit": MAX_RESPONSE_CHARS,
        }
    )

    excerpt_limit = min(4000, MAX_RESPONSE_CHARS // 2)
    excerpt = content[:excerpt_limit]
    truncated_metadata["output_excerpt_length"] = len(excerpt)

    logger.warning(
        "Clink truncated %s output: original=%d chars exceeds limit=%d; excerpt_length=%d",
        client.name,
        len(content),
        MAX_RESPONSE_CHARS,
        len(excerpt),
    )

    blocks: list[ContentBlock] = []
    full_output_note = "The full output was suppressed to stay within MCP response caps."
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="clink-full-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        truncated_metadata["output_full_file"] = tmp_path
        blocks.append(file_block(tmp_path))
        full_output_note = f"The full output was saved to {tmp_path} (attached as a resource link)."
    except OSError:
        logger.debug("Could not persist full clink output for %s", client.name, exc_info=True)

    message = (
        f"CLI '{client.name}' produced {len(content)} characters, exceeding the configured clink limit "
        f"({MAX_RESPONSE_CHARS} characters). {full_output_note} "
        "Please narrow the request (review fewer files, summarize results) or run the CLI directly for the full log.\n\n"
        f"--- Begin excerpt ({len(excerpt)} of {len(content)} chars) ---\n{excerpt}\n--- End excerpt ---"
    )

    return message, truncated_metadata, blocks


def _extract_summary(content: str) -> str | None:
    match = SUMMARY_PATTERN.search(content)
    if not match:
        return None
    summary = match.group(1).strip()
    return summary or None


def _prune_metadata(
    metadata: dict[str, Any],
    client: ResolvedCLIClient,
    *,
    reason: str,
) -> dict[str, Any]:
    cleaned = dict(metadata)
    events = cleaned.pop("events", None)
    if events is not None:
        cleaned[f"events_removed_for_{reason}"] = True
        logger.debug(
            "Clink dropped %s events metadata for %s response (%s)",
            client.name,
            reason,
            type(events).__name__,
        )
    return cleaned


class CLinkRequest(BaseModel):
    """Request model for clink tool."""

    prompt: str = Field(..., description="Prompt forwarded to the target CLI.")
    cli_name: str | None = Field(
        default=None,
        description="Configured CLI client name to invoke. Defaults to the first configured CLI if omitted.",
    )
    role: str | None = Field(
        default=None,
        description="Optional role preset defined in the CLI configuration (defaults to 'default').",
    )
    absolute_file_paths: list[str] = Field(
        default_factory=list,
        description=COMMON_FIELD_DESCRIPTIONS["absolute_file_paths"],
    )
    images: list[str] = Field(
        default_factory=list,
        description=COMMON_FIELD_DESCRIPTIONS["images"],
    )
    continuation_id: str | None = Field(
        default=None,
        description=COMMON_FIELD_DESCRIPTIONS["continuation_id"],
    )
    wait: bool = Field(
        default=True,
        description=WAIT_FIELD_DESCRIPTION,
    )


class CLinkTool(SimpleTool):
    """Bridge MCP requests to configured CLI agents.

    Schema metadata is cached at construction time and execution relies on the shared
    SimpleTool hooks for conversation memory. Prompt preparation is customised so we
    pass instructions and file references suitable for another CLI agent.
    """

    def __init__(self) -> None:
        # Cache registry metadata so the schema surfaces concrete enum values.
        self._registry = get_registry()
        self._cli_names = self._registry.list_clients()
        self._role_map: dict[str, list[str]] = {name: self._registry.list_roles(name) for name in self._cli_names}
        self._all_roles: list[str] = sorted({role for roles in self._role_map.values() for role in roles})
        if "gemini" in self._cli_names:
            self._default_cli_name = "gemini"
        else:
            self._default_cli_name = self._cli_names[0] if self._cli_names else None
        self._active_system_prompt: str = ""
        super().__init__()

    def get_name(self) -> str:
        return "clink"

    def get_description(self) -> str:
        return (
            "Link a request to an external AI CLI (Gemini CLI, Qwen CLI, etc.) through PAL MCP to reuse "
            "their capabilities inside existing workflows."
        )

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": True}

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.BALANCED

    def get_default_temperature(self) -> float:
        return TEMPERATURE_BALANCED

    def get_system_prompt(self) -> str:
        return self._active_system_prompt or ""

    def get_request_model(self):
        return CLinkRequest

    def get_input_schema(self) -> dict[str, Any]:
        # Surface configured CLI names and roles directly in the schema so MCP clients
        # (and downstream agents) can discover available options without consulting
        # a separate registry call.
        role_descriptions = []
        for name in self._cli_names:
            roles = ", ".join(sorted(self._role_map.get(name, ["default"]))) or "default"
            role_descriptions.append(f"{name}: {roles}")

        if role_descriptions:
            cli_available = ", ".join(self._cli_names) if self._cli_names else "(none configured)"
            default_text = (
                f" Default: {self._default_cli_name}." if self._default_cli_name and len(self._cli_names) <= 1 else ""
            )
            cli_description = (
                "Configured CLI client name (from conf/cli_clients). Available: " + cli_available + default_text
            )
            role_description = (
                "Optional role preset defined for the selected CLI (defaults to 'default'). Roles per CLI: "
                + "; ".join(role_descriptions)
            )
        else:
            cli_description = "Configured CLI client name (from conf/cli_clients)."
            role_description = "Optional role preset defined for the selected CLI (defaults to 'default')."

        properties = {
            "prompt": {
                "type": "string",
                "description": "User request forwarded to the CLI (conversation context is pre-applied).",
            },
            "cli_name": {
                "type": "string",
                "enum": self._cli_names,
                "description": cli_description,
            },
            "role": {
                "type": "string",
                "enum": self._all_roles or ["default"],
                "description": role_description,
            },
            "absolute_file_paths": SchemaBuilder.SIMPLE_FIELD_SCHEMAS["absolute_file_paths"],
            "images": SchemaBuilder.COMMON_FIELD_SCHEMAS["images"],
            "continuation_id": SchemaBuilder.COMMON_FIELD_SCHEMAS["continuation_id"],
            "wait": {
                "type": "boolean",
                "default": True,
                "description": WAIT_FIELD_DESCRIPTION,
            },
        }

        schema = {
            "type": "object",
            "properties": properties,
            "required": ["prompt"],
            "additionalProperties": False,
        }

        if len(self._cli_names) > 1:
            schema["required"].append("cli_name")

        return schema

    def get_tool_fields(self) -> dict[str, dict[str, Any]]:
        """Unused by clink because we override the schema end-to-end."""
        return {}

    async def execute(self, arguments: dict[str, Any]) -> list[ContentBlock]:
        self._current_arguments = arguments
        request = self.get_request_model()(**arguments)

        path_error = self._validate_file_paths(request)
        if path_error:
            self._raise_tool_error(path_error)

        selected_cli = request.cli_name or self._default_cli_name
        if not selected_cli:
            self._raise_tool_error("No CLI clients are configured for clink.")

        try:
            client_config = self._registry.get_client(selected_cli)
        except KeyError as exc:
            self._raise_tool_error(str(exc))

        try:
            role_config = client_config.get_role(request.role)
        except KeyError as exc:
            self._raise_tool_error(str(exc))

        absolute_file_paths = self.get_request_files(request)
        images = self.get_request_images(request)
        continuation_id = self.get_request_continuation_id(request)

        self._model_context = arguments.get("_model_context")

        system_prompt_text = role_config.prompt_path.read_text(encoding="utf-8")
        include_system_prompt = not self._use_external_system_prompt(client_config)

        # Mailbox-addressable workers get a unique instance id (base, base-2, ...) so
        # concurrent spawns of the same client stay individually reachable, and a
        # roster entry so their parent can list its own stack.
        mailbox_agent_id = client_config.env.get("PAL_AGENT_ID")
        if mailbox_agent_id:
            mailbox_agent_id = uniquify_agent_id(mailbox_agent_id)
            client_config = client_config.model_copy(
                update={"env": {**client_config.env, "PAL_AGENT_ID": mailbox_agent_id}}
            )

        try:
            prompt_text = await self._prepare_prompt_for_role(
                request,
                role_config,
                system_prompt=system_prompt_text,
                include_system_prompt=include_system_prompt,
                mailbox_agent_id=mailbox_agent_id,
            )
        except Exception as exc:
            logger.exception("Failed to prepare clink prompt")
            self._raise_tool_error(f"Failed to prepare prompt: {exc}")

        agent = create_agent(client_config)

        # Detached jobs return before any mailbox roster registration: the task runs
        # remotely, so there is no live worker to address and no turn end to unregister.
        if not request.wait:
            return await self._dispatch_async(
                agent,
                client_config,
                role_config,
                prompt_text,
                system_prompt_text,
                continuation_id,
            )

        if mailbox_agent_id:
            register_agent(
                mailbox_agent_id,
                parent_id=current_agent_id(),
                client=client_config.name,
                role=role_config.name,
            )
        try:
            result = await agent.run(
                role=role_config,
                prompt=prompt_text,
                system_prompt=system_prompt_text if system_prompt_text.strip() else None,
                files=absolute_file_paths,
                images=images,
            )
        except CLIAgentError as exc:
            metadata = self._build_error_metadata(client_config, exc)
            self._raise_tool_error(
                f"CLI '{client_config.name}' execution failed: {exc}",
                metadata=metadata,
            )
        finally:
            if mailbox_agent_id:
                unregister_agent(mailbox_agent_id)

        metadata = self._build_success_metadata(client_config, role_config, result)
        if mailbox_agent_id:
            metadata["mailbox_agent_id"] = mailbox_agent_id
        metadata = _prune_metadata(metadata, client_config, reason="normal")

        content, artifact_blocks = extract_artifacts(result.parsed.content)
        if artifact_blocks:
            metadata["artifacts_attached"] = len(artifact_blocks)

        content, metadata, limit_blocks = apply_output_limit(
            client_config,
            content,
            metadata,
        )

        model_info = {
            "provider": client_config.name,
            "model_name": result.parsed.metadata.get("model_used"),
        }

        if continuation_id:
            try:
                self._record_assistant_turn(continuation_id, content, request, model_info)
            except Exception:
                logger.debug("Failed to record assistant turn for continuation %s", continuation_id, exc_info=True)

        continuation_offer = self._create_continuation_offer(request, model_info)
        if continuation_offer:
            tool_output = self._create_continuation_offer_response(
                content,
                continuation_offer,
                request,
                model_info,
            )
            tool_output.metadata = self._merge_metadata(tool_output.metadata, metadata)
        else:
            tool_output = ToolOutput(
                status="success",
                content=content,
                content_type="text",
                metadata=metadata,
            )

        return [TextContent(type="text", text=tool_output.model_dump_json()), *artifact_blocks, *limit_blocks]

    async def _dispatch_async(
        self,
        agent,
        client_config: ResolvedCLIClient,
        role_config: ResolvedCLIRole,
        prompt_text: str,
        system_prompt_text: str,
        continuation_id: str | None,
    ) -> list[ContentBlock]:
        """Dispatch without polling and return a job record immediately (wait=false)."""
        if not isinstance(agent, DispatchAgent) or not client_config.dispatch or not client_config.dispatch.poll_args:
            self._raise_tool_error(
                f"CLI '{client_config.name}' runs synchronously; wait=false requires a client with a "
                "pollable dispatch configuration (dispatch.poll_args)."
            )

        try:
            handle, sanitized_command = await agent.dispatch_only(
                role=role_config,
                prompt=prompt_text,
                system_prompt=system_prompt_text if system_prompt_text.strip() else None,
            )
        except CLIAgentError as exc:
            metadata = self._build_error_metadata(client_config, exc)
            self._raise_tool_error(
                f"CLI '{client_config.name}' dispatch failed: {exc}",
                metadata=metadata,
            )

        record = jobs.create_job(
            cli_name=client_config.name,
            role=role_config.name,
            handle=handle,
            continuation_id=continuation_id,
            sanitized_command=sanitized_command,
        )

        tool_output = ToolOutput(
            status="success",
            content=(
                f"Dispatched task to CLI '{client_config.name}' (handle: {handle}). "
                f"The task is running remotely; this session is not blocked. "
                f"Use the 'jobs' tool with job_id '{record.job_id}' to poll (action 'status') "
                f"and fetch the result (action 'collect')."
            ),
            content_type="text",
            metadata={
                "job_id": record.job_id,
                "dispatch_handle": handle,
                "cli_name": client_config.name,
                "role": role_config.name,
                "state": jobs.STATE_RUNNING,
                "command": sanitized_command,
            },
        )
        return [TextContent(type="text", text=tool_output.model_dump_json())]

    async def prepare_prompt(self, request) -> str:
        client_config = self._registry.get_client(request.cli_name)
        role_config = client_config.get_role(request.role)
        system_prompt_text = role_config.prompt_path.read_text(encoding="utf-8")
        include_system_prompt = not self._use_external_system_prompt(client_config)
        return await self._prepare_prompt_for_role(
            request,
            role_config,
            system_prompt=system_prompt_text,
            include_system_prompt=include_system_prompt,
            mailbox_agent_id=client_config.env.get("PAL_AGENT_ID"),
        )

    async def _prepare_prompt_for_role(
        self,
        request: CLinkRequest,
        role: ResolvedCLIRole,
        *,
        system_prompt: str,
        include_system_prompt: bool,
        mailbox_agent_id: str | None = None,
    ) -> str:
        """Load the role prompt and assemble the final user message."""
        self._active_system_prompt = system_prompt
        try:
            user_content = self.handle_prompt_file_with_fallback(request).strip()
            guidance = self._agent_capabilities_guidance()
            file_section = self._format_file_references(self.get_request_files(request))

            sections: list[str] = []
            active_prompt = self.get_system_prompt().strip()
            if include_system_prompt and active_prompt:
                sections.append(active_prompt)
            sections.append(guidance)
            if mailbox_agent_id:
                sections.append(self._mailbox_guidance(mailbox_agent_id))
            sections.append("=== USER REQUEST ===\n" + user_content)
            if file_section:
                sections.append("=== FILE REFERENCES ===\n" + file_section)
            sections.append("Provide your response below using your own CLI tools as needed:")
            return "\n\n".join(sections)
        finally:
            self._active_system_prompt = ""

    def _mailbox_guidance(self, agent_id: str) -> str:
        parent_id = current_agent_id()
        return (
            "=== PAL MAILBOX ===\n"
            f"This session has a PAL mailbox; your agent id is '{agent_id}' and the session that spawned "
            f"you is '{parent_id}'. Other agent sessions can send you messages while you work: pending "
            "messages are injected automatically at turn end when delivery hooks are installed, and you "
            "can read them on demand with the PAL 'mailbox' tool (action 'check'). Use 'list' to see "
            f"your live sibling workers. Treat messages from '{parent_id}' as authoritative follow-up "
            "instructions for this task. Send a message (action 'send') only when it changes another "
            "agent's work: blockers, interface or contract changes, shared-file conflicts, or results a "
            "sibling is waiting on. Your final result is returned automatically; do not mail it."
        )

    def _use_external_system_prompt(self, client: ResolvedCLIClient) -> bool:
        runner_name = (client.runner or client.name).lower()
        return runner_name == "claude"

    def _build_success_metadata(
        self,
        client: ResolvedCLIClient,
        role: ResolvedCLIRole,
        result: AgentOutput,
    ) -> dict[str, Any]:
        """Capture execution metadata for successful CLI calls."""
        metadata: dict[str, Any] = {
            "cli_name": client.name,
            "role": role.name,
            "command": result.sanitized_command,
            "duration_seconds": round(result.duration_seconds, 3),
            "parser": result.parser_name,
            "return_code": result.returncode,
        }
        metadata.update(result.parsed.metadata)

        if result.stderr.strip():
            metadata.setdefault("stderr", result.stderr.strip())
        if result.output_file_content and "raw" not in metadata:
            metadata["raw_output_file"] = result.output_file_content
        return metadata

    def _merge_metadata(self, base: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base or {})
        merged.update(extra)
        return merged

    def _build_error_metadata(self, client: ResolvedCLIClient, exc: CLIAgentError) -> dict[str, Any]:
        """Assemble metadata for failed CLI calls."""
        metadata: dict[str, Any] = {
            "cli_name": client.name,
            "return_code": exc.returncode,
        }
        if exc.stdout:
            metadata["stdout"] = exc.stdout.strip()
        if exc.stderr:
            metadata["stderr"] = exc.stderr.strip()
        return metadata

    def _raise_tool_error(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        error_output = ToolOutput(status="error", content=message, content_type="text", metadata=metadata)
        raise ToolExecutionError(error_output.model_dump_json())

    def _agent_capabilities_guidance(self) -> str:
        return (
            "You are operating through the Gemini CLI agent. You have access to your full suite of "
            "CLI capabilities—including launching web searches, reading files, and using any other "
            "available tools. Gather current information yourself and deliver the final answer without "
            "asking the PAL MCP host to perform searches or file reads."
        )

    def _format_file_references(self, files: list[str]) -> str:
        if not files:
            return ""

        references: list[str] = []
        for file_path in files:
            try:
                path = Path(file_path)
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                size = stat.st_size
                references.append(f"- {file_path} (last modified {modified}, {size} bytes)")
            except OSError:
                references.append(f"- {file_path} (unavailable)")
        return "\n".join(references)
