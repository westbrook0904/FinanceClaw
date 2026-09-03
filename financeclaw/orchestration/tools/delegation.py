"""把 Workflow 与领域 Agent 包装为受治理 delegation Tool 的实现。

属于 orchestration/tools 治理层的委托实现模块：顶层 Agent 通过调用
delegation Tool 以 typed handoff 方式把任务交给 Workflow 或领域
Agent（独立 child thread/run 执行），工具执行经 LangGraph interrupt
挂起等待子运行结果回填。
"""

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
    """领域 Agent 委托 Tool 的入参模型：任务描述加上运行时上下文。

    使用场景：作为 ``DelegationTool`` 的 args_schema 供 LangChain 校验
    与生成参数 schema；runtime 字段由运行时自动填充，Agent 只提供
    继承自 AgentDelegationInput 的任务字段。

    Attributes:
        runtime: LangChain 运行时句柄，提供执行上下文与 tool_call_id，
            用于构造委托的父运行定位与幂等键。
        task: 继承字段，交给领域 Agent 的有界任务描述，1~8000 字符。
        context_refs: 继承字段，可随任务传递的上下文引用集合，最多
            32 个。

    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    runtime: ToolRuntime[ExecutionContext]


class DelegationTool(BaseTool):
    """把任务委托给 Workflow 或领域 Agent 的 Tool，经 interrupt 挂起等待结果。

    使用场景：由 workflow_delegation_tool 与 agent_delegation_tool 工厂
    构造，暴露给顶层 Agent 调用；执行时校验会话轮次上下文、生成确定
    性 handoff_id、发出 typed handoff 并通过 LangGraph interrupt 挂起，
    直到编排层以匹配的 DelegationResult 恢复执行，工具返回结果的
    JSON 序列化。

    Attributes:
        handoff_kind: 委托类型，区分 WORKFLOW 与 AGENT 两种目标。
        target_id: 委托目标标识，即 workflow_id 或 agent_id。
        name: 继承字段，由工厂按委托类型与目标生成的工具名。
        description: 继承字段，展示给顶层 Agent 的委托用途说明。
        args_schema: 继承字段，入参模型，见各工厂函数的 schema 定义。

    """

    handoff_kind: DelegationKind
    target_id: str

    def _run(
        self,
        *,
        runtime: ToolRuntime[ExecutionContext],
        **arguments: Any,
    ) -> str:
        """执行一次委托：生成 handoff、挂起等待子运行并校验回填结果。

        Args:
            runtime: LangChain 运行时句柄，提供执行上下文与 tool_call_id。
            **arguments: Agent 提供的委托入参；Workflow 委托为工作流
                入参，Agent 委托须包含 task（可选 context_refs）。

        Returns:
            DelegationResult 的 JSON 序列化，包含子运行标识与输出。

        Raises:
            ValueError: 缺少会话轮次上下文，或恢复的结果与挂起的
                handoff 不匹配。

        """
        # 1. 规范化执行上下文，并要求必须处于某个会话轮次之内。
        context = (
            runtime.context
            if isinstance(runtime.context, ExecutionContext)
            else ExecutionContext.model_validate(runtime.context)
        )
        if context.conversation_id is None or context.turn_id is None:
            raise ValueError("delegation requires a conversation turn context")
        tool_call_id = runtime.tool_call_id or "unknown-tool-call"
        # 2. 由父运行、工具调用与目标共同派生确定性 handoff_id（幂等键）。
        handoff_id = _handoff_id(
            parent_run_id=context.run_id,
            tool_call_id=tool_call_id,
            kind=self.handoff_kind,
            target_id=self.target_id,
        )
        # 3. 按委托类型构造 typed handoff 载荷。
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
        # 4. 通过 LangGraph interrupt 挂起本工具，等编排层启动子运行后回填结果。
        resumed = interrupt(handoff.model_dump(mode="json"))
        # 5. 校验回填结果的委托标识、类型与目标均与挂起时一致。
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
        """异步入口：直接复用同步实现，委托逻辑经 interrupt 统一挂起。"""
        return self._run(runtime=runtime, **arguments)


def workflow_delegation_tool(definition: WorkflowDefinition) -> ManagedTool:
    """把已发布 Workflow 定义包装为受治理的委托 Tool。

    Args:
        definition: 已发布的工作流定义，提供 workflow_id、入参模型
            与所需作用域。

    Returns:
        包装完成的 ManagedTool，tool_id 为按约定生成的委托工具名。

    """
    name = delegation_tool_name(DelegationKind.WORKFLOW, definition.workflow_id)
    # 1. 动态派生入参模型：继承工作流入参并注入运行时上下文字段。
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
    # 2. 与固定治理元数据一起包装为受治理 Tool。
    return ManagedTool(
        tool=tool,
        governance=_governance(name, definition.required_scopes),
    )


def agent_delegation_tool(profile: AgentProfile) -> ManagedTool:
    """把可委托的领域 Agent 配置包装为受治理的委托 Tool。

    Args:
        profile: 领域 Agent 的配置画像，须标记为可委托。

    Returns:
        包装完成的 ManagedTool，tool_id 为按约定生成的委托工具名。

    Raises:
        ValueError: profile 未标记为 delegatable。

    """
    # 1. 仅允许显式声明可委托的 Agent 配置成为委托 Tool。
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
    # 2. 与固定治理元数据一起包装为受治理 Tool，作用域沿用 Agent 配置。
    return ManagedTool(tool=tool, governance=_governance(name, profile.required_scopes))


def _governance(tool_id: str, required_scopes: frozenset[str]) -> ToolGovernance:
    """构造 delegation Tool 的固定治理元数据。

    委托类 Tool 统一禁止 API 直连调用，副作用记为 delegation；
    审批由委托流程自身的授权与审计保证，此处不再叠加 ALWAYS 审批。

    Args:
        tool_id: 委托工具名，作为治理 tool_id。
        required_scopes: 被委托目标（Workflow 或 Agent）要求的作用域。

    Returns:
        配置完成的 ToolGovernance 实例。

    """
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
    """由委托要素派生确定性的 handoff 标识，用作委托幂等键。

    Args:
        parent_run_id: 发起委托的父运行标识。
        tool_call_id: 触发委托的工具调用标识。
        kind: 委托类型。
        target_id: 委托目标标识。

    Returns:
        形如 ``delegation-<32 位十六进制>`` 的确定性标识；同一父运行
        中重复的工具调用会得到相同标识，从而保证重放安全。

    """
    digest = sha256(
        json.dumps(
            [parent_run_id, tool_call_id, kind.value, target_id],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"delegation-{digest[:32]}"
