"""定义授权、执行与记忆操作使用的不可变审计事件。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditEventType(StrEnum):
    """定义审计事件Type。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        TOOL_ALLOWED: 表示 `tool_allowed` 这一受限枚举值。
        TOOL_DENIED: 表示 `tool_denied` 这一受限枚举值。
        TOOL_APPROVAL_REQUESTED: 表示 `tool_approval_requested` 这一受限枚举值。
        TOOL_APPROVED: 表示 `tool_approved` 这一受限枚举值。
        TOOL_REJECTED: 表示 `tool_rejected` 这一受限枚举值。
        FINANCIAL_TOOL_EXECUTED: 表示 `financial_tool_executed` 这一受限枚举值。
        FINANCIAL_TOOL_FAILED: 表示 `financial_tool_failed` 这一受限枚举值。
        MEMORY_PROPOSED: 表示 `memory_proposed` 这一受限枚举值。
        MEMORY_COMMITTED: 表示 `memory_committed` 这一受限枚举值。
        MEMORY_SUPERSEDED: 表示 `memory_superseded` 这一受限枚举值。
        MEMORY_REVOKED: 表示 `memory_revoked` 这一受限枚举值。
        MEMORY_DELETED: 表示 `memory_deleted` 这一受限枚举值。
        WORKFLOW_STARTED: 表示 `workflow_started` 这一受限枚举值。
        WORKFLOW_INTERRUPTED: 表示 `workflow_interrupted` 这一受限枚举值。
        WORKFLOW_APPROVED: 表示 `workflow_approved` 这一受限枚举值。
        WORKFLOW_REJECTED: 表示 `workflow_rejected` 这一受限枚举值。
        WORKFLOW_COMPLETED: 表示 `workflow_completed` 这一受限枚举值。
        WORKFLOW_FAILED: 表示 `workflow_failed` 这一受限枚举值。
        DELEGATION_REQUESTED: 表示 `delegation_requested` 这一受限枚举值。
        DELEGATION_STARTED: 表示 `delegation_started` 这一受限枚举值。
        DELEGATION_INTERRUPTED: 表示 `delegation_interrupted` 这一受限枚举值。
        DELEGATION_COMPLETED: 表示 `delegation_completed` 这一受限枚举值。
        DELEGATION_FAILED: 表示 `delegation_failed` 这一受限枚举值。
        DELEGATION_DELIVERED: 表示 `delegation_delivered` 这一受限枚举值。
    """

    TOOL_ALLOWED = "tool.allowed"
    TOOL_DENIED = "tool.denied"
    TOOL_APPROVAL_REQUESTED = "tool.approval_requested"
    TOOL_APPROVED = "tool.approved"
    TOOL_REJECTED = "tool.rejected"
    FINANCIAL_TOOL_EXECUTED = "financial_tool.executed"
    FINANCIAL_TOOL_FAILED = "financial_tool.failed"
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_COMMITTED = "memory.committed"
    MEMORY_SUPERSEDED = "memory.superseded"
    MEMORY_REVOKED = "memory.revoked"
    MEMORY_DELETED = "memory.deleted"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_INTERRUPTED = "workflow.interrupted"
    WORKFLOW_APPROVED = "workflow.approved"
    WORKFLOW_REJECTED = "workflow.rejected"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    DELEGATION_REQUESTED = "delegation.requested"
    DELEGATION_STARTED = "delegation.started"
    DELEGATION_INTERRUPTED = "delegation.interrupted"
    DELEGATION_COMPLETED = "delegation.completed"
    DELEGATION_FAILED = "delegation.failed"
    DELEGATION_DELIVERED = "delegation.delivered"


class AuditRecord(BaseModel):
    """定义审计的持久化记录。

    适用场景：
        用于跨步骤保存不可变事实，并支持持久化或审计重放。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        audit_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        event_type: 事件的语义类型，供消费者选择处理逻辑。
        occurred_at: 该生命周期事件发生的 UTC 时间。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tool_call_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        resource_type: 被审批、审计或事件关联的资源类别。
        resource_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        resource_version: 运行固定使用的版本，用于审计复现。
        action: 审批点准备执行的动作名称。
        decision: 审批人或策略引擎作出的结构化决定。
        policy_version: 作出决策时使用的策略版本。
        payload_hash: 事件载荷的稳定哈希，用于完整性核对。
        evidence_refs: 支撑该记忆事实的消息或外部证据引用。
        artifact_refs: 本次运行、审计或事件关联的制品标识集合。
        metadata: 随运行或记录保存的非业务控制信息。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(default_factory=lambda: f"audit-{uuid4().hex}")
    event_type: AuditEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: str
    subject_id: str
    conversation_id: str | None = None
    turn_id: str
    run_id: str
    tool_call_id: str | None = None
    resource_type: str = "tool"
    resource_id: str
    resource_version: str
    action: str
    decision: str
    policy_version: str
    payload_hash: str
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
