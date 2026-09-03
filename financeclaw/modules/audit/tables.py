"""声明审计事件的 SQLAlchemy 持久化映射。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class AuditRecordRow(Base):
    """定义审计RecordRow。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
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
        metadata_json: 经 JSON 编码后持久化的附加审计元数据。
    """

    __tablename__ = "audit_records"
    __table_args__ = (
        Index("ix_audit_owner_time", "tenant_id", "subject_id", "occurred_at"),
        Index("ix_audit_run", "run_id", "event_type"),
    )

    audit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_version: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
