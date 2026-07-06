"""Pydantic models for clink configuration and runtime structures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator


class DispatchConfig(BaseModel):
    """Configuration for CLIs that dispatch asynchronous (remote/background) tasks.

    When present, the client runs a dispatch -> poll -> collect lifecycle instead of a
    single blocking invocation. Without ``poll_args`` the dispatch acknowledgement is
    returned immediately (fire-and-forget).
    """

    handle_pattern: str = Field(
        ...,
        description="Regex applied to dispatch output to extract the task handle (first capture group, or the full match).",
    )
    poll_args: list[str] = Field(
        default_factory=list,
        description="Arguments (appended to the executable) used to poll task status. '{handle}' is substituted. Empty means fire-and-forget.",
    )
    poll_interval_seconds: PositiveInt = Field(
        default=15,
        description="Seconds to wait between poll attempts.",
    )
    done_pattern: str | None = Field(
        default=None,
        description="Regex matched against poll output that signals task completion.",
    )
    failed_pattern: str | None = Field(
        default=None,
        description="Regex matched against poll output that signals task failure.",
    )
    collect_args: list[str] = Field(
        default_factory=list,
        description="Arguments used to fetch the final result once done. '{handle}' is substituted. Empty means the last poll output is the result.",
    )

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> DispatchConfig:
        if self.poll_args and not self.done_pattern:
            raise ValueError("dispatch.done_pattern is required when dispatch.poll_args is configured")
        if not self.poll_args:
            unused = [
                name
                for name, value in (
                    ("done_pattern", self.done_pattern),
                    ("failed_pattern", self.failed_pattern),
                    ("collect_args", self.collect_args),
                )
                if value
            ]
            if unused:
                raise ValueError(
                    f"dispatch.{', dispatch.'.join(unused)} require dispatch.poll_args (without polling, "
                    "the dispatch acknowledgement is returned immediately)"
                )
        return self


class OutputCaptureConfig(BaseModel):
    """Optional configuration for CLIs that write output to disk."""

    flag_template: str = Field(..., description="Template used to inject the output path, e.g. '--output {path}'.")
    cleanup: bool = Field(
        default=True,
        description="Whether the temporary file should be removed after reading.",
    )


class CLIRoleConfig(BaseModel):
    """Role-specific configuration loaded from JSON manifests."""

    prompt_path: str | None = Field(
        default=None,
        description="Path to the prompt file that seeds this role.",
    )
    role_args: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None)

    @field_validator("role_args", mode="before")
    @classmethod
    def _ensure_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value]
        raise TypeError("role_args must be a list of strings or a single string")


class CLIClientConfig(BaseModel):
    """Raw CLI client configuration before internal defaults are applied."""

    name: str
    command: str | None = None
    working_dir: str | None = None
    additional_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: PositiveInt | None = Field(default=None)
    roles: dict[str, CLIRoleConfig] = Field(default_factory=dict)
    output_to_file: OutputCaptureConfig | None = None
    parser: str | None = None
    runner: str | None = None
    dispatch: DispatchConfig | None = None

    @field_validator("additional_args", mode="before")
    @classmethod
    def _ensure_args_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value]
        raise TypeError("additional_args must be a list of strings or a single string")


class ResolvedCLIRole(BaseModel):
    """Runtime representation of a CLI role with resolved prompt path."""

    name: str
    prompt_path: Path
    role_args: list[str] = Field(default_factory=list)
    description: str | None = None


class ResolvedCLIClient(BaseModel):
    """Runtime configuration after merging defaults and validating paths."""

    name: str
    executable: list[str]
    working_dir: Path | None
    internal_args: list[str] = Field(default_factory=list)
    config_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int
    parser: str
    runner: str | None = None
    roles: dict[str, ResolvedCLIRole]
    output_to_file: OutputCaptureConfig | None = None
    dispatch: DispatchConfig | None = None

    def list_roles(self) -> list[str]:
        return list(self.roles.keys())

    def get_role(self, role_name: str | None) -> ResolvedCLIRole:
        key = role_name or "default"
        if key not in self.roles:
            available = ", ".join(sorted(self.roles.keys()))
            raise KeyError(f"Role '{role_name}' not configured for CLI '{self.name}'. Available roles: {available}")
        return self.roles[key]
