"""把 Agent 与工作流委派目标包装成可治理工具。"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import ConfigDict

from financeclaw.kernel import DataClassification, ExecutionContext
from financeclaw.modules.delegation.models import (
    AgentDelegationInput,
    AgentHandoff,
    DelegationKind,
    DelegationResult,
    WorkflowHandoff,
)
from financeclaw.modules.delegation.naming import delegation_tool_name
from financeclaw.modules.workflows import WorkflowDefinition
from financeclaw.orchestration.agents.profiles import AgentProfile

from .governance import (
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


class AgentDelegationToolInput(AgentDelegationInput):
    """定义Agent委派工具的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        runtime: LangChain 注入的可信工具运行上下文，不由模型生成。
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    runtime: ToolRuntime[ExecutionContext]


class DelegationTool(BaseTool):
    """定义委派工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        handoff_kind: 委派目标种类，决定交给工作流还是 Agent。
        target_id: 解析前或解析后的目标稳定标识。
    """

    handoff_kind: DelegationKind
    target_id: str

    def _run(
        self,
        *,
        runtime: ToolRuntime[ExecutionContext],
        **arguments: Any,
    ) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
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
        """执行工具的异步实现，保持与同步入口相同的业务语义。"""
        return self._run(runtime=runtime, **arguments)


def workflow_delegation_tool(definition: WorkflowDefinition) -> ManagedTool:
    """根据工作流定义生成可中断的委派工具及治理元数据。"""
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
    """根据专业 Agent 配置生成可中断的委派工具及治理元数据。"""
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
    """构造委派工具统一使用的权限、副作用、审批和审计策略。"""
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
    """由父运行、工具调用和目标生成稳定移交标识，保证重放幂等。"""
    digest = sha256(
        json.dumps(
            [parent_run_id, tool_call_id, kind.value, target_id],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"delegation-{digest[:32]}"
