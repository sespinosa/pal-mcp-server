"""Dispatch/poll agent for asynchronous CLI backends.

Runs a dispatch -> poll -> collect lifecycle for CLIs that submit work to a remote or
background service (cloud coding agents, task queues) instead of answering inline:

1. **Dispatch** the task using the regular configured command. The task handle is
   extracted from the output via ``dispatch.handle_pattern``.
2. **Poll** (optional) by running ``dispatch.poll_args`` with ``{handle}`` substituted
   until ``dispatch.done_pattern`` (or ``dispatch.failed_pattern``) matches, bounded by
   the client's ``timeout_seconds`` budget.
3. **Collect** (optional) the final result via ``dispatch.collect_args``; otherwise the
   last poll output is parsed as the result.

Without ``poll_args`` the dispatch acknowledgement itself is returned immediately
(fire-and-forget), which suits services that expose no status API and deliver their
result out of band (e.g. as a pull request).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence

from clink.models import DispatchConfig, ResolvedCLIRole
from clink.parsers import ParserError

from .base import AgentOutput, BaseCLIAgent, CLIAgentError

PROMPT_PLACEHOLDER = "{prompt}"
HANDLE_PLACEHOLDER = "{handle}"

# Handles are substituted into poll/collect argv; keep them to a conservative charset
# (never leading '-') so dispatch output echoing the prompt can't smuggle in CLI flags.
_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")


class DispatchAgent(BaseCLIAgent):
    """Execute asynchronous CLI backends through a dispatch/poll/collect lifecycle."""

    async def run(
        self,
        *,
        role: ResolvedCLIRole,
        prompt: str,
        system_prompt: str | None = None,
        files: Sequence[str],
        images: Sequence[str],
    ) -> AgentOutput:
        _ = (files, images)
        dispatch = self.client.dispatch
        if dispatch is None:
            raise CLIAgentError(f"CLI '{self.client.name}' uses the dispatch runner but has no dispatch configuration")

        start_time = time.monotonic()
        deadline = start_time + self.client.timeout_seconds

        command = self._build_command(role=role, system_prompt=system_prompt)
        command = self._resolve_executable(command)

        # CLIs that cannot read the prompt from stdin may take it as an argument via
        # the {prompt} placeholder. The sanitized command keeps the placeholder so the
        # (potentially large) prompt never lands in response metadata.
        sanitized_command = list(command)
        input_text: str | None = prompt
        if any(PROMPT_PLACEHOLDER in arg for arg in command):
            command = [arg.replace(PROMPT_PLACEHOLDER, prompt) for arg in command]
            input_text = None

        return_code, stdout, stderr = await self._execute_command(
            command,
            input_text=input_text,
            timeout_seconds=self._remaining(deadline),
        )
        if return_code != 0:
            raise CLIAgentError(
                f"CLI '{self.client.name}' dispatch failed with status {return_code}",
                returncode=return_code,
                stdout=stdout,
                stderr=stderr,
            )

        handle = self._extract_handle(dispatch, stdout, stderr)
        self._logger.info("Dispatched task via CLI '%s' (handle: %s)", self.client.name, handle)

        poll_attempts = 0
        if dispatch.poll_args:
            stdout, stderr, poll_attempts = await self._poll_until_done(dispatch, handle, deadline)
            if dispatch.collect_args:
                stdout, stderr = await self._run_phase(dispatch.collect_args, handle, deadline, phase="collect")

        return self._finalize(
            stdout=stdout,
            stderr=stderr,
            sanitized_command=sanitized_command,
            duration_seconds=time.monotonic() - start_time,
            handle=handle,
            poll_attempts=poll_attempts,
        )

    def _remaining(self, deadline: float, handle: str | None = None) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            suffix = ""
            if handle:
                suffix = f" while waiting for task '{handle}'; the remote task may still be running"
            raise CLIAgentError(
                f"CLI '{self.client.name}' timed out after {self.client.timeout_seconds} seconds{suffix}",
                returncode=None,
            )
        return remaining

    def _extract_handle(self, dispatch: DispatchConfig, stdout: str, stderr: str) -> str:
        combined = "\n".join(part for part in (stdout, stderr) if part)
        match = re.search(dispatch.handle_pattern, combined)
        if not match:
            raise CLIAgentError(
                f"Could not extract task handle from CLI '{self.client.name}' dispatch output "
                f"using pattern '{dispatch.handle_pattern}'",
                stdout=stdout,
                stderr=stderr,
            )
        handle = match.group(1) if match.groups() else match.group(0)
        if not handle or not _HANDLE_PATTERN.match(handle):
            raise CLIAgentError(
                f"CLI '{self.client.name}' dispatch produced an unusable task handle {handle!r} "
                f"(pattern '{dispatch.handle_pattern}'). Anchor the pattern to the exact id format.",
                stdout=stdout,
                stderr=stderr,
            )
        return handle

    async def _run_phase(
        self,
        args: list[str],
        handle: str,
        deadline: float,
        *,
        phase: str,
    ) -> tuple[str, str]:
        command = list(self.client.executable)
        command.extend(arg.replace(HANDLE_PLACEHOLDER, handle) for arg in args)
        command = self._resolve_executable(command)

        return_code, stdout, stderr = await self._execute_command(
            command,
            input_text=None,
            timeout_seconds=self._remaining(deadline, handle),
        )
        if return_code != 0:
            raise CLIAgentError(
                f"CLI '{self.client.name}' {phase} command for task '{handle}' failed with status {return_code}",
                returncode=return_code,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout, stderr

    async def _poll_until_done(
        self,
        dispatch: DispatchConfig,
        handle: str,
        deadline: float,
    ) -> tuple[str, str, int]:
        attempts = 0
        while True:
            attempts += 1
            stdout, stderr = await self._run_phase(dispatch.poll_args, handle, deadline, phase="poll")
            combined = "\n".join(part for part in (stdout, stderr) if part)

            if dispatch.failed_pattern and re.search(dispatch.failed_pattern, combined):
                raise CLIAgentError(
                    f"CLI '{self.client.name}' reported task '{handle}' as failed",
                    stdout=stdout,
                    stderr=stderr,
                )
            if re.search(dispatch.done_pattern, combined):
                return stdout, stderr, attempts

            # Leave at least ~1s of budget for one final poll after sleeping.
            remaining = self._remaining(deadline, handle)
            delay = min(dispatch.poll_interval_seconds, max(remaining - 1.0, 0.5))
            self._logger.debug("Task '%s' not complete after poll #%d; sleeping %.1fs", handle, attempts, delay)
            await asyncio.sleep(delay)

    def _finalize(
        self,
        *,
        stdout: str,
        stderr: str,
        sanitized_command: list[str],
        duration_seconds: float,
        handle: str,
        poll_attempts: int,
    ) -> AgentOutput:
        try:
            parsed = self._parser.parse(stdout, stderr)
        except ParserError as exc:
            raise CLIAgentError(
                f"Failed to parse output from CLI '{self.client.name}': {exc}",
                returncode=0,
                stdout=stdout,
                stderr=stderr,
            ) from exc

        parsed.metadata.setdefault("dispatch_handle", handle)
        if poll_attempts:
            parsed.metadata.setdefault("poll_attempts", poll_attempts)

        return AgentOutput(
            parsed=parsed,
            sanitized_command=sanitized_command,
            returncode=0,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            parser_name=self._parser.name,
            output_file_content=None,
        )
