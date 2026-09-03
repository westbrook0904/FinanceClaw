"""审计记录的 ORM 表定义。

位于 audit 模块的持久化层：定义 ``audit_records`` 表结构，供仓储层写入与查询，
与 Outbox 事件表配合实现审计记录与事件的同事务落盘。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class AuditRecordRow(Base):
    """``audit_records`` 表的 ORM 映射，保存一条不可变的永久审计记录。

    使用场景：由 SqlAlchemyAuditRepository 在追加审计时写入；通过租户/主体与
    时间、运行标识与事件类型两类索引支持审计查询与追溯。

    Attributes:
        audit_id: 审计记录唯一标识，主键，最长 128 字符。
        event_type: 事件类型字符串（``AuditEventType`` 的值），最长 64 字符，非空。
        occurred_at: 事件发生时间（带时区），默认为当前 UTC 时间，非空。
        tenant_id: 租户标识，最长 128 字符，非空。
        subject_id: 主体标识，最长 128 字符，非空。
        conversation_id: 关联会话标识，最长 128 字符；非会话场景为 NULL。
        turn_id: 事件所属的对话轮次标识，最长 128 字符，非空。
        run_id: 事件所属的 Agent 运行标识，最长 128 字符，非空。
        tool_call_id: 关联的工具调用标识，最长 128 字符；仅工具类事件存在。
        resource_type: 被操作资源类型，最长 64 字符，非空。
        resource_id: 被操作资源标识，最长 128 字符，非空。
        resource_version: 被操作资源版本，最长 32 字符，非空。
        action: 对资源执行的动作名称，最长 64 字符，非空。
        decision: 策略判定结果，最长 64 字符，非空。
        policy_version: 作出判定时使用的策略版本，最长 64 字符，非空。
        payload_hash: 事件负载的 SHA256 摘要，64 字符，非空。
        evidence_refs: 证据引用标识列表（JSON 数组），默认为空列表，非空。
        artifact_refs: 关联 Artifact 标识列表（JSON 数组），默认为空列表，非空。
        metadata_json: 附加结构化元数据（JSON 对象），映射到列 ``metadata``，
            默认为空字典，非空。

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
