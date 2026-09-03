"""Deterministic target-to-deployed-graph resolution."""

from dataclasses import dataclass
from typing import Any

from financeclaw.agents import AgentProfileCatalog
from financeclaw.contracts import AgentTarget, RunRequest, ToolTarget, WorkflowTarget
from financeclaw.tools import ToolCatalog


class TargetResolutionError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    kind: str
    assistant_id: str
    input: dict[str, Any]
    target_id: str
    target_version: str


class TargetResolver:
    def __init__(
        self,
        *,
        tool_catalog: ToolCatalog,
        agent_profiles: AgentProfileCatalog,
        default_agent_id: str = "finance_agent",
        default_agent_version: str = "1.0.0",
    ) -> None:
        self.tool_catalog = tool_catalog
        self.agent_profiles = agent_profiles
        self.default_agent_id = default_agent_id
        self.default_agent_version = default_agent_version

    def resolve(self, request: RunRequest) -> ResolvedTarget:
        target = request.target
        if target is None:
            profile = self.agent_profiles.resolve(self.default_agent_id, self.default_agent_version)
            return ResolvedTarget(
                kind="agent",
                assistant_id="finance_agent",
                input={"messages": [{"role": "user", "content": request.message}]},
                target_id=profile.agent_id,
                target_version=profile.version,
            )
        if isinstance(target, AgentTarget):
            try:
                profile = self.agent_profiles.resolve(target.agent_id, target.version)
            except LookupError as exc:
                raise TargetResolutionError(str(exc)) from exc
            return ResolvedTarget(
                kind="agent",
                assistant_id="finance_agent",
                input={"messages": [{"role": "user", "content": request.message}]},
                target_id=profile.agent_id,
                target_version=profile.version,
            )
        if isinstance(target, ToolTarget):
            try:
                managed = self.tool_catalog.resolve(target.tool_id, target.version)
            except LookupError as exc:
                raise TargetResolutionError(str(exc)) from exc
            if not managed.governance.direct_invocation:
                raise TargetResolutionError(
                    f"tool is only available inside the governed Agent path: {target.tool_id}"
                )
            return ResolvedTarget(
                kind="tool",
                assistant_id="direct_tool",
                input={
                    "tool_id": managed.governance.tool_id,
                    "version": managed.governance.version,
                    "arguments": target.arguments,
                },
                target_id=managed.governance.tool_id,
                target_version=managed.governance.version,
            )
        if isinstance(target, WorkflowTarget):
            raise TargetResolutionError("published workflows are introduced in Stage 4")
        raise TypeError("unsupported request target")
