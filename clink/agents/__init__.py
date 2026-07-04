"""Agent factory for clink CLI integrations."""

from __future__ import annotations

from clink.models import ResolvedCLIClient

from .base import AgentOutput, BaseCLIAgent, CLIAgentError
from .claude import ClaudeAgent
from .codex import CodexAgent
from .dispatch import DispatchAgent
from .gemini import GeminiAgent

_AGENTS: dict[str, type[BaseCLIAgent]] = {
    "gemini": GeminiAgent,
    "codex": CodexAgent,
    "claude": ClaudeAgent,
}


def create_agent(client: ResolvedCLIClient) -> BaseCLIAgent:
    # Dispatch lifecycles are selected by configuration, not by name, so a CLI that
    # merely happens to be called "dispatch" cannot pick up the wrong runner.
    if client.dispatch is not None:
        return DispatchAgent(client)
    agent_key = (client.runner or client.name).lower()
    agent_cls = _AGENTS.get(agent_key, BaseCLIAgent)
    return agent_cls(client)


__all__ = [
    "AgentOutput",
    "BaseCLIAgent",
    "CLIAgentError",
    "create_agent",
]
