"""LangChain Tools that suspend a parent Agent for an external child run."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import ConfigDict

from financeclaw.agents.profiles import AgentProfile
from financeclaw.contracts import DataClassification, ExecutionContext
from financeclaw.tools import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)
from financeclaw.workflows import WorkflowDefinition

from .models import (
    AgentDelegationInput,
    AgentHandoff,
    DelegationKind,
    DelegationResult,
    WorkflowHandoff,
)
from .naming import delegation_tool_name


class AgentDelegationToolInput(AgentDelegationInput):
    """Full Tool schema; runtime is injected and hidden from the model."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    runtime: ToolRuntime[ExecutionContext]


class DelegationTool(BaseTool):
    """Emit a typed interrupt and return only after its child run finishes."""

    handoff_kind: DelegationKind
    target_id: str

    def _run(
        self,
        *,
        runtime: ToolRuntime[ExecutionContext],
        **arguments: Any,
    ) -> str:
        context = (
            runtime.context
            if isinstance(runtime.context, ExecutionContext)
            else ExecutionContext.model_validate(runtime.context)
        )
        if context.conversation_id is None or context.turn_id is None:
            raise ValueError("delegation requires a conversation turn context")
        tool_call_id = runtime.tool_call_id or "unknown-tool-call"
        handoff_id = _handoff_id(
            parent_run_id=context.run_id,
            tool_call_id=tool_call_id,
            kind=self.handoff_kind,
            target_id=self.target_id,
        )
        if self.handoff_kind is DelegationKind.WORKFLOW:
            handoff = WorkflowHandoff(
                handoff_id=handoff_id,
                parent_run_id=context.run_id,
                parent_turn_id=context.turn_id,
                conversation_id=context.conversation_id,
                workflow_id=self.target_id,
                arguments=arguments,
            )
        else:
            handoff = AgentHandoff(
                handoff_id=handoff_id,
                parent_run_id=context.run_id,
                parent_turn_id=context.turn_id,
                conversation_id=context.conversation_id,
                agent_id=self.target_id,
                task=str(arguments["task"]),
                context_refs=tuple(arguments.get("context_refs", ())),
            )
        resumed = interrupt(handoff.model_dump(mode="json"))
        result = DelegationResult.model_validate(resumed)
        if (
            result.delegation_id != handoff_id
            or result.kind is not self.handoff_kind
            or result.target_id != self.target_id
        ):
            raise ValueError("delegation result does not match the suspended handoff")
        return result.model_dump_json()

    async def _arun(
        self,
        *,
        runtime: ToolRuntime[ExecutionContext],
        **arguments: Any,
    ) -> str:
        return self._run(runtime=runtime, **arguments)


def workflow_delegation_tool(definition: WorkflowDefinition) -> ManagedTool:
    name = delegation_tool_name(DelegationKind.WORKFLOW, definition.workflow_id)
    input_schema = type(
        f"{definition.input_schema.__name__}DelegationToolInput",
        (definition.input_schema,),
        {
            "__annotations__": {"runtime": ToolRuntime[ExecutionContext]},
            "model_config": ConfigDict(
                extra="forbid",
                frozen=True,
                arbitrary_types_allowed=True,
            ),
        },
    )
    tool = DelegationTool(
        name=name,
        description=(
            f"Start published workflow {definition.workflow_id}. "
            "Use this for deterministic, checkpointed work that needs its own child run."
        ),
        args_schema=input_schema,
        handoff_kind=DelegationKind.WORKFLOW,
        target_id=definition.workflow_id,
    )
    return ManagedTool(
        tool=tool,
        governance=_governance(name, definition.required_scopes),
    )


def agent_delegation_tool(profile: AgentProfile) -> ManagedTool:
    if not profile.delegatable:
        raise ValueError("only delegatable AgentProfiles can become delegation Tools")
    name = delegation_tool_name(DelegationKind.AGENT, profile.agent_id)
    tool = DelegationTool(
        name=name,
        description=(
            f"Delegate a bounded task to domain Agent {profile.agent_id}: {profile.description}"
        ),
        args_schema=AgentDelegationToolInput,
        handoff_kind=DelegationKind.AGENT,
        target_id=profile.agent_id,
    )
    return ManagedTool(tool=tool, governance=_governance(name, profile.required_scopes))


def _governance(tool_id: str, required_scopes: frozenset[str]) -> ToolGovernance:
    return ToolGovernance(
        tool_id=tool_id,
        version="1.0.0",
        side_effect=SideEffect.DELEGATION,
        idempotency=Idempotency.KEY_REQUIRED,
        risk_level=RiskLevel.MEDIUM,
        required_scopes=required_scopes,
        approval=ApprovalMode.NONE,
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.CONFIDENTIAL,
        retry_profile=RetryProfile.NONE,
        audit_level=AuditLevel.FULL,
        direct_invocation=False,
        allowed_data_classes=frozenset(
            {
                DataClassification.PUBLIC,
                DataClassification.INTERNAL,
                DataClassification.CONFIDENTIAL,
            }
        ),
    )


def _handoff_id(
    *, parent_run_id: str, tool_call_id: str, kind: DelegationKind, target_id: str
) -> str:
    digest = sha256(
        json.dumps(
            [parent_run_id, tool_call_id, kind.value, target_id],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"delegation-{digest[:32]}"
