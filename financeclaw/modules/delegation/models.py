"""定义子 Agent 或工作流委派的请求、状态和持久化记录。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ContextReference = Annotated[str, Field(min_length=1, max_length=256)]


class FrozenDelegationModel(BaseModel):
    """定义不可变委派模型。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DelegationKind(StrEnum):
    """委派目标属于工作流还是专业 Agent。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        WORKFLOW: 显式调用或委派目标是确定性工作流。
        AGENT: 显式调用或委派目标是专业 Agent。
    """

    WORKFLOW = "workflow"
    AGENT = "agent"


class DelegationStatus(StrEnum):
    """委派从创建到交付或失败的生命周期状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        REQUESTED: 表示 `requested` 这一受限枚举值。
        PENDING: 操作已创建但尚未开始处理。
        RUNNING: 操作正在执行且尚未产生终态结果。
        INTERRUPTED: 运行停在可恢复检查点，等待外部决定。
        COMPLETED: 操作已成功完成并可读取最终结果。
        REJECTED: 表示 `rejected` 这一受限枚举值。
        FAILED: 操作已失败，错误信息应记录在对应字段。
        DELIVERED: 表示 `delivered` 这一受限枚举值。
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
    """定义工作流Handoff。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        schema_version: 记录结构版本，用于兼容演进和历史数据解析。
        handoff_id: 由父运行和工具调用确定生成的幂等委派标识。
        kind: 记录或目标的语义类别。
        parent_run_id: 发起委派的父运行标识。
        parent_turn_id: 发起委派的父会话轮次标识。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        workflow_id: 工作流的稳定标识。
        arguments: 传给目标工具或工作流的已解析参数。
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
    """定义AgentHandoff。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        schema_version: 记录结构版本，用于兼容演进和历史数据解析。
        handoff_id: 由父运行和工具调用确定生成的幂等委派标识。
        kind: 记录或目标的语义类别。
        parent_run_id: 发起委派的父运行标识。
        parent_turn_id: 发起委派的父会话轮次标识。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        agent_id: Agent 配置的稳定标识。
        task: 交给子 Agent 或工作流处理的自然语言任务说明。
        context_refs: 父运行显式传给子目标的上下文引用。
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


HandoffRequest = Annotated[WorkflowHandoff | AgentHandoff, Field(discriminator="kind")]
HANDOFF_ADAPTER = TypeAdapter(HandoffRequest)


class DelegationResult(FrozenDelegationModel):
    """定义委派的执行结果。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        schema_version: 记录结构版本，用于兼容演进和历史数据解析。
        delegation_id: 一次父子运行委派的稳定标识。
        kind: 记录或目标的语义类别。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
        child_run_id: 实际执行委派任务的子运行标识。
        status: 当前生命周期状态，决定记录允许的后续操作。
        output: 运行完成后的结构化输出；尚未完成时为空。
        error: 失败原因的稳定文本；成功或未结束时为空。
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
    """定义父运行向子目标移交任务的持久化状态。

    适用场景：
        用于跨步骤保存不可变事实，并支持持久化或审计重放。

    属性：
        delegation_id: 一次父子运行委派的稳定标识。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        parent_turn_id: 发起委派的父会话轮次标识。
        parent_run_id: 发起委派的父运行标识。
        kind: 记录或目标的语义类别。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
        arguments: 传给目标工具或工作流的已解析参数。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        request_fingerprint: 委派或工作流请求的稳定指纹，用于幂等冲突检测。
        authorization_decision: 执行前记录的策略授权结果。
        policy_version: 作出决策时使用的策略版本。
        child_run_id: 实际执行委派任务的子运行标识。
        child_thread_id: 子 Agent 保存检查点与消息的线程标识。
        child_server_run_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        status: 当前生命周期状态，决定记录允许的后续操作。
        output_payload: 运行终态时保存的结构化输出快照。
        error: 失败原因的稳定文本；成功或未结束时为空。
        created_at: 记录创建时间，统一按 UTC 解释。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        completed_at: 进入成功或失败终态的时间；未结束时为空。
        delivered_at: 该生命周期事件发生的 UTC 时间。
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
        """判断当前状态是否已经进入不可继续推进的终态。"""
        return self.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }


class AgentDelegationInput(BaseModel):
    """定义Agent委派的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        task: 交给子 Agent 或工作流处理的自然语言任务说明。
        context_refs: 父运行显式传给子目标的上下文引用。
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8_000)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=32)
