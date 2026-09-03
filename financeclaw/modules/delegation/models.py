"""委派（delegation）领域的 Pydantic 数据契约：typed handoff、结果与永久记录。

本模块定义顶层 finance_agent 发起委派时的请求载荷（WorkflowHandoff 或
AgentHandoff）、子任务完成后的 DelegationResult，以及与 ``delegations`` 表
对应的 DelegationRecord 持久化快照，是 delegation 子系统的模型层。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# 单条上下文引用（如证据、artifact 的标识或 URI），长度限制在 1 到 256 个字符。
ContextReference = Annotated[str, Field(min_length=1, max_length=256)]


class FrozenDelegationModel(BaseModel):
    """委派模块 Pydantic 模型的公共基类：禁止额外字段且实例不可变。

    使用场景：handoff 请求、委派结果与委派记录都继承该基类，保证数据在工具
    调用、interrupt/resume 往返与持久化过程中保持结构稳定、不被意外篡改。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DelegationKind(StrEnum):
    """委派目标的种类，区分把任务交给可检查点的 Workflow 还是只读领域 Agent。

    使用场景：父 Agent 的委派工具按种类构造 handoff，仓库按 ``kind`` 落库，
    服务层据此选择 child 启动路径（Workflow 走 workflow service，Agent 走
    agent server client）。

    Attributes:
        WORKFLOW: 委派给已发布 Workflow（如 ``portfolio_review@1.0.0``），
            child 是带审批中断能力的确定性工作流运行。
        AGENT: 委派给只读领域 Agent（如 ``market_research_agent@1.0.0``），
            child 是 agent server 上的独立 thread/run。

    """

    WORKFLOW = "workflow"
    AGENT = "agent"


class DelegationStatus(StrEnum):
    """委派生命周期状态，覆盖从受理请求到结果交付父 Agent 的完整状态机。

    使用场景：仓库写入 ``delegations.status``；服务层据此判断是否需要重启
    child run、是否继续同步 child 状态，以及结果是否已交付而无需重复投递。

    Attributes:
        REQUESTED: 委派请求已受理并落库（幂等创建），child run 尚未启动。
        PENDING: child 身份已就绪（Agent 委派已分配 child run/thread），
            等待实际启动执行。
        RUNNING: child run 已绑定并正在执行。
        INTERRUPTED: child 运行进入审批中断，等待 resume 恢复执行。
        COMPLETED: child 执行成功，结果已写入 output_payload。
        REJECTED: 委派被审批流程拒绝。
        FAILED: child 执行失败，原因记录在 error 字段。
        DELIVERED: 终态结果已以 DelegationResult 形式交付父 Agent 汇总。

    """

    REQUESTED = "requested"
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    DELIVERED = "delivered"


class WorkflowHandoff(FrozenDelegationModel):
    """委派给 Workflow 的 typed handoff 请求载荷。

    使用场景：顶层 finance_agent 调用形如 ``delegate_workflow__<workflow_id>``
    的委派工具时构造该模型，经 interrupt 暂停父 Agent，由委派服务启动目标
    Workflow 的 child run，完成后再以 DelegationResult 恢复父 Agent。

    Attributes:
        schema_version: handoff 载荷结构版本，当前固定为 1。
        handoff_id: 本次委派的唯一标识，同时作为 ``delegations`` 表主键，
            用于幂等受理与结果匹配。
        kind: 判别字段，恒为 DelegationKind.WORKFLOW。
        parent_run_id: 发起委派的父 Agent 运行 ID。
        parent_turn_id: 发起委派的父会话轮次 ID。
        conversation_id: 父子运行所属的会话 ID。
        workflow_id: 目标 Workflow 标识（如 ``portfolio_review``）。
        arguments: 透传给 Workflow 的启动参数字典。

    """

    schema_version: Literal[1] = 1
    handoff_id: str = Field(min_length=1, max_length=128)
    kind: Literal[DelegationKind.WORKFLOW] = DelegationKind.WORKFLOW
    parent_run_id: str = Field(min_length=1, max_length=128)
    parent_turn_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


class AgentHandoff(FrozenDelegationModel):
    """委派给只读领域 Agent 的 typed handoff 请求载荷。

    使用场景：顶层 finance_agent 调用形如 ``delegate_agent__<agent_id>`` 的
    委派工具时构造该模型；委派服务据此为领域 Agent 准备独立 child
    thread/run，提交一段有界的任务描述与可选上下文引用。

    Attributes:
        schema_version: handoff 载荷结构版本，当前固定为 1。
        handoff_id: 本次委派的唯一标识，同时作为 ``delegations`` 表主键，
            用于幂等受理与结果匹配。
        kind: 判别字段，恒为 DelegationKind.AGENT。
        parent_run_id: 发起委派的父 Agent 运行 ID。
        parent_turn_id: 发起委派的父会话轮次 ID。
        conversation_id: 父子运行所属的会话 ID。
        agent_id: 目标领域 Agent 标识（如 ``market_research_agent``）。
        task: 提交给领域 Agent 的任务描述，长度 1 到 8000 字符。
        context_refs: 可选上下文引用列表，最多 32 条，指向任务所需的
            证据或 artifact。

    """

    schema_version: Literal[1] = 1
    handoff_id: str = Field(min_length=1, max_length=128)
    kind: Literal[DelegationKind.AGENT] = DelegationKind.AGENT
    parent_run_id: str = Field(min_length=1, max_length=128)
    parent_turn_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=8_000)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=32)


# typed handoff 判别联合：按 ``kind`` 字段在 WorkflowHandoff 与 AgentHandoff
# 之间自动区分，供服务层统一解析两种委派请求。
HandoffRequest = Annotated[WorkflowHandoff | AgentHandoff, Field(discriminator="kind")]
# HandoffRequest 的可复用 TypeAdapter，用于解析并校验 JSON 形式的 handoff 载荷。
HANDOFF_ADAPTER = TypeAdapter(HandoffRequest)


class DelegationResult(FrozenDelegationModel):
    """委派子任务完成后的结构化结果，用于恢复父 Agent 并汇总输出。

    使用场景：委派服务在 child run 进入终态后构造该模型，通过
    interrupt/resume 通道交回父 Agent 的委派工具；工具会校验 delegation_id、
    kind 与 target_id 与挂起的 handoff 一致后才采纳结果。

    Attributes:
        schema_version: 结果载荷结构版本，当前固定为 1。
        delegation_id: 对应委派记录 ID（即原 handoff_id）。
        kind: 委派种类，与原 handoff 保持一致。
        target_id: 委派目标标识（workflow_id 或 agent_id）。
        target_version: 被委派目标的版本号（如 ``1.0.0``）。
        child_run_id: 承接任务的 child 运行 ID。
        status: 终态结果，只能是 completed、rejected 或 failed。
        output: child 成功产出的输出载荷，失败或被拒时为 None。
        error: 失败或被拒的原因说明，成功时为 None。

    """

    schema_version: Literal[1] = 1
    delegation_id: str
    kind: DelegationKind
    target_id: str
    target_version: str
    child_run_id: str
    status: Literal["completed", "rejected", "failed"]
    output: dict[str, Any] | None = None
    error: str | None = None


class DelegationRecord(FrozenDelegationModel):
    """一次委派在 ``delegations`` 表中的完整持久化快照。

    使用场景：委派服务的各阶段（受理、启动、状态同步、交付）都以该模型为
    数据载体；父 Agent 恢复时依据它构造 DelegationResult，并为全程提供
    审计与幂等依据。

    Attributes:
        delegation_id: 委派唯一标识（与 handoff_id 相同），作记录主键。
        tenant_id: 租户 ID，用于多租户隔离。
        subject_id: 主体（用户）ID，配合租户做归属校验。
        conversation_id: 委派所属会话 ID。
        parent_turn_id: 发起委派的父会话轮次 ID。
        parent_run_id: 发起委派的父 Agent 运行 ID。
        kind: 委派种类（Workflow 或 Agent）。
        target_id: 目标标识（workflow_id 或 agent_id）。
        target_version: 目标版本号（如 ``1.0.0``）。
        arguments: 委派参数原文（Workflow 参数或 Agent 的 task 等）。
        arguments_hash: arguments 的 SHA-256 十六进制摘要，用于审计比对。
        request_fingerprint: 请求指纹（覆盖会话、父子运行、目标与参数的
            SHA-256 摘要），防止同一 handoff_id 被复用于不同请求。
        authorization_decision: 授权决策，受理前已完成校验，恒为 ``allowed``。
        policy_version: 受理时使用的委派策略版本。
        child_run_id: child 运行 ID，child 未启动时为 None。
        child_thread_id: child 独立 thread ID，child 未启动时为 None。
        child_server_run_id: agent server 侧运行 ID，仅 Agent 委派有值。
        status: 当前委派状态，取值见 DelegationStatus。
        output_payload: child 成功输出的载荷，未完成时为 None。
        error: 失败或被拒原因，未出错时为 None。
        created_at: 记录创建时间（UTC）。
        updated_at: 最近一次状态更新时间（UTC）。
        completed_at: 首次进入完成、拒绝或失败终态的时间，未终态为 None。
        delivered_at: 首次标记 DELIVERED 的时间，未交付为 None。

    """

    delegation_id: str
    tenant_id: str
    subject_id: str
    conversation_id: str
    parent_turn_id: str
    parent_run_id: str
    kind: DelegationKind
    target_id: str
    target_version: str
    arguments: dict[str, Any]
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_decision: Literal["allowed"] = "allowed"
    policy_version: str = "delegation-policy/1.0.0"
    child_run_id: str | None = None
    child_thread_id: str | None = None
    child_server_run_id: str | None = None
    status: DelegationStatus
    output_payload: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    delivered_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        """判断委派是否已进入终态（完成、拒绝、失败或已交付）。"""
        return self.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }


class AgentDelegationInput(BaseModel):
    """委派给只读领域 Agent 时工具入参的标准结构。

    使用场景：作为 Agent 委派工具 args_schema 的基类，约束父 Agent 只能提交
    一段任务描述与可选的上下文引用列表；允许传入额外字段以兼容工具运行时。

    Attributes:
        task: 提交给领域 Agent 的任务描述，长度 1 到 8000 字符。
        context_refs: 可选上下文引用列表，最多 32 条，每条为
            ContextReference。

    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8_000)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=32)
