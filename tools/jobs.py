"""jobs tool - poll and collect asynchronous clink dispatch jobs.

Companion to ``clink``'s ``wait=false`` mode: dispatching returns a ``job_id``
immediately, and this tool resolves it later. Each ``status`` call runs exactly
one poll cycle, so the calling model owns the polling cadence and no MCP request
ever blocks on a remote task. The tool is only registered when at least one
configured CLI client has a pollable dispatch configuration.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.types import ContentBlock, TextContent

from clink import get_registry
from clink.agents import CLIAgentError, create_agent
from clink.agents.dispatch import DispatchAgent
from tools.clink import apply_output_limit, extract_artifacts
from tools.models import ToolModelCategory, ToolOutput
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool
from tools.shared.exceptions import ToolExecutionError
from utils import jobs as job_store

logger = logging.getLogger(__name__)

FIELD_DESCRIPTIONS = {
    "action": (
        "list: show all known jobs. status: run one poll cycle for a job and report its state. "
        "collect: fetch the final result of a job (polls once first if it was still running)."
    ),
    "job_id": "Job id returned by clink with wait=false (required for status and collect).",
}


class JobsTool(BaseTool):
    """Inspect and collect asynchronous clink dispatch jobs."""

    def get_name(self) -> str:
        return "jobs"

    def get_description(self) -> str:
        return (
            "Poll and collect asynchronous clink jobs dispatched with wait=false. "
            "Use action 'status' to check on a job_id and 'collect' to fetch its final result."
        )

    def get_annotations(self) -> dict[str, Any] | None:
        return {"readOnlyHint": True}

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "collect"],
                    "description": FIELD_DESCRIPTIONS["action"],
                },
                "job_id": {
                    "type": "string",
                    "description": FIELD_DESCRIPTIONS["job_id"],
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

    def format_response(self, response: str, request: ToolRequest, model_info: dict | None = None) -> str:
        return response

    async def execute(self, arguments: dict[str, Any]) -> list[ContentBlock]:
        action = arguments.get("action")
        if action == "list":
            return self._list()
        if action not in ("status", "collect"):
            self._raise_error(f"Unknown action {action!r}; expected list, status, or collect.")

        job_id = arguments.get("job_id")
        if not job_id:
            self._raise_error(f"Action '{action}' requires a job_id.")

        record = job_store.get_job(job_id)
        if record is None:
            self._raise_error(
                f"Unknown or expired job id '{job_id}'. Jobs are kept for "
                f"{job_store.JOB_TTL_SECONDS // 3600} hours; use action 'list' to see live jobs."
            )

        if action == "status":
            return await self._status(record)
        return await self._collect(record)

    # === actions ===

    def _list(self) -> list[ContentBlock]:
        records = job_store.list_jobs()
        if not records:
            content = "No clink jobs. Dispatch one with clink wait=false."
        else:
            lines = ["Known clink jobs (newest last):"]
            for record in records:
                age_minutes = (time.time() - record.created_at) / 60
                lines.append(
                    f"- {record.job_id}: {record.state} — cli={record.cli_name} role={record.role} "
                    f"handle={record.handle} age={age_minutes:.0f}m"
                )
            content = "\n".join(lines)
        tool_output = ToolOutput(
            status="success",
            content=content,
            content_type="text",
            metadata={"jobs": [self._record_metadata(record) for record in records]},
        )
        return [TextContent(type="text", text=tool_output.model_dump_json())]

    async def _status(self, record: job_store.JobRecord) -> list[ContentBlock]:
        if record.state == job_store.STATE_RUNNING:
            record = await self._poll(record)
        return self._state_response(record)

    async def _collect(self, record: job_store.JobRecord) -> list[ContentBlock]:
        if record.state == job_store.STATE_COLLECTED and record.result is not None:
            tool_output = ToolOutput(
                status="success",
                content=record.result.get("content"),
                content_type="text",
                metadata=record.result.get("metadata"),
            )
            return [TextContent(type="text", text=tool_output.model_dump_json())]

        if record.state == job_store.STATE_RUNNING:
            record = await self._poll(record)

        if record.state == job_store.STATE_RUNNING:
            return self._state_response(record, note="Not done yet; try again later.")
        if record.state == job_store.STATE_FAILED:
            self._raise_error(
                f"Job '{record.job_id}' failed on CLI '{record.cli_name}' "
                f"(handle: {record.handle}). Last output:\n{record.output_tail}",
                metadata=self._record_metadata(record),
            )

        client, agent = self._resolve_agent(record)
        try:
            result = await agent.collect(record.handle)
        except CLIAgentError as exc:
            # Keep state=done so collect can be retried after transient failures.
            self._raise_error(
                f"Collect for job '{record.job_id}' failed: {exc}",
                metadata=self._record_metadata(record),
            )

        metadata: dict[str, Any] = self._record_metadata(record)
        metadata.update(result.parsed.metadata)
        metadata["parser"] = result.parser_name
        metadata["command"] = result.sanitized_command

        content, artifact_blocks = extract_artifacts(result.parsed.content)
        if artifact_blocks:
            metadata["artifacts_attached"] = len(artifact_blocks)
        content, metadata, limit_blocks = apply_output_limit(client, content, metadata)

        if record.continuation_id:
            try:
                from utils.conversation_memory import add_turn

                add_turn(record.continuation_id, "assistant", content, tool_name=self.get_name())
            except Exception:
                logger.debug(
                    "Failed to record collected job %s in continuation %s",
                    record.job_id,
                    record.continuation_id,
                    exc_info=True,
                )

        record.state = job_store.STATE_COLLECTED
        record.result = {"content": content, "metadata": metadata}
        job_store.save_job(record)

        tool_output = ToolOutput(status="success", content=content, content_type="text", metadata=metadata)
        return [TextContent(type="text", text=tool_output.model_dump_json()), *artifact_blocks, *limit_blocks]

    # === helpers ===

    async def _poll(self, record: job_store.JobRecord) -> job_store.JobRecord:
        _, agent = self._resolve_agent(record)
        try:
            state, stdout, stderr = await agent.check(record.handle)
        except CLIAgentError as exc:
            # A failing poll command is not a failed task; keep the record running.
            self._raise_error(
                f"Poll for job '{record.job_id}' failed: {exc}",
                metadata=self._record_metadata(record),
            )

        record.state = state
        record.poll_attempts += 1
        combined = "\n".join(part for part in (stdout, stderr) if part)
        record.output_tail = combined[-job_store.OUTPUT_TAIL_CHARS :]
        if state == job_store.STATE_FAILED:
            record.error = record.output_tail
        job_store.save_job(record)
        return record

    def _resolve_agent(self, record: job_store.JobRecord):
        try:
            client = get_registry().get_client(record.cli_name)
        except KeyError:
            self._raise_error(
                f"CLI client '{record.cli_name}' for job '{record.job_id}' is no longer configured; "
                "restore its configuration under conf/cli_clients (or ~/.pal/cli_clients) to resolve the job."
            )
        agent = create_agent(client)
        if not isinstance(agent, DispatchAgent):
            self._raise_error(
                f"CLI client '{record.cli_name}' no longer has a dispatch configuration; "
                f"job '{record.job_id}' cannot be resolved."
            )
        return client, agent

    def _state_response(self, record: job_store.JobRecord, note: str | None = None) -> list[ContentBlock]:
        parts = [f"Job '{record.job_id}' is {record.state} (poll #{record.poll_attempts})."]
        if note:
            parts.append(note)
        if record.state == job_store.STATE_DONE:
            parts.append("Fetch the result with action 'collect'.")
        tool_output = ToolOutput(
            status="success",
            content=" ".join(parts),
            content_type="text",
            metadata=self._record_metadata(record),
        )
        return [TextContent(type="text", text=tool_output.model_dump_json())]

    def _record_metadata(self, record: job_store.JobRecord) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "cli_name": record.cli_name,
            "role": record.role,
            "dispatch_handle": record.handle,
            "state": record.state,
            "poll_attempts": record.poll_attempts,
            "created_at": record.created_at,
            "continuation_id": record.continuation_id,
        }

    def _raise_error(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        error_output = ToolOutput(status="error", content=message, content_type="text", metadata=metadata)
        raise ToolExecutionError(error_output.model_dump_json())
