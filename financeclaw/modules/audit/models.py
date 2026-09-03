"""审计记录的领域模型定义。

位于 audit 模块的模型层：定义审计事件类型枚举与不可变的审计记录模型，覆盖工具
调用、审批、记忆写入、工作流与委派的完整生命周期。
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditEventType(StrEnum):
    """审计事件类型枚举，覆盖各领域对象完整生命周期中的关键事件。

    使用场景：写入 AuditRecord 时标记事件种类；仓储层把其字符串值持久化到审计
    表与 Outbox 事件中，供检索与投递。

    Attributes:
        TOOL_ALLOWED: 工具调用被策略允许。
        TOOL_DENIED: 工具调用被策略拒绝。
        TOOL_APPROVAL_REQUESTED: 工具调用触发审批请求。
        TOOL_APPROVED: 工具调用审批通过。
        TOOL_REJECTED: 工具调用审批被驳回。
        FINANCIAL_TOOL_EXECUTED: 金融工具执行成功。
        FINANCIAL_TOOL_FAILED: 金融工具执行失败。
        MEMORY_PROPOSED: 记忆写入提案已生成。
        MEMORY_COMMITTED: 记忆提案已提交生效。
        MEMORY_SUPERSEDED: 既有记忆被新记忆取代。
        MEMORY_REVOKED: 记忆被撤销。
        MEMORY_DELETED: 记忆被删除。
        WORKFLOW_STARTED: 工作流运行已启动。
        WORKFLOW_INTERRUPTED: 工作流运行被中断。
        WORKFLOW_APPROVED: 工作流审批获得通过。
        WORKFLOW_REJECTED: 工作流审批被驳回。
        WORKFLOW_COMPLETED: 工作流运行完成。
        WORKFLOW_FAILED: 工作流运行失败。
        DELEGATION_REQUESTED: 委派请求已登记。
        DELEGATION_STARTED: 委派子任务已启动。
        DELEGATION_INTERRUPTED: 委派子任务被中断。
        DELEGATION_COMPLETED: 委派子任务执行完成。
        DELEGATION_FAILED: 委派子任务执行失败。
        DELEGATION_DELIVERED: 委派结果已回递给父运行。

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
    """一条不可变的永久审计记录，描述"谁在何时对什么资源做了什么决定"。

    使用场景：工具调用、审批、记忆写入、工作流与委派等关键动作发生时构造，
    由 AuditRepository 与 Outbox 事件在同一事务中落盘，作为合规与追溯依据。

    Attributes:
        audit_id: 审计记录唯一标识，形如 ``audit-<uuid4 hex>``，默认自动生成。
        event_type: 事件类型（AuditEventType），标记该记录覆盖的生命周期事件。
        occurred_at: 事件发生时间（UTC 带时区），默认为构造时的当前时间。
        tenant_id: 租户标识，用于多租户数据隔离。
        subject_id: 主体标识，与 tenant_id 共同界定审计记录的归属。
        conversation_id: 关联会话标识；非会话场景为 None。
        turn_id: 事件所属的对话轮次标识。
        run_id: 事件所属的 Agent 运行标识。
        tool_call_id: 关联的工具调用标识；仅工具类事件存在，其余场景为 None。
        resource_type: 被操作资源类型，默认为 ``tool``。
        resource_id: 被操作资源标识。
        resource_version: 被操作资源的版本，用于策略与语义的可追溯。
        action: 对资源执行的动作名称。
        decision: 策略判定结果（如允许、拒绝、需审批）。
        policy_version: 作出判定时使用的策略版本。
        payload_hash: 事件负载的摘要哈希，在不落明文的情况下固化证据。
        evidence_refs: 证据引用标识列表（如消息、文件引用），默认为空。
        artifact_refs: 关联 Artifact 标识列表，默认为空。
        metadata: 附加结构化元数据，默认为空字典。

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
