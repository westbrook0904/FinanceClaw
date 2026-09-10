"""通知订阅、业务事件与独立投递回执；审计 Outbox 不作为发送凭据。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base


def utcnow() -> datetime:
    """统一 UTC 时间。"""
    return datetime.now(UTC)


class NotificationTargetRow(Base):
    """一个根任务只有一个原消息订阅，审批回复不能重新绑定最终目标。"""

    __tablename__ = "notification_targets"
    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run_executions.run_id"), unique=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("channel_conversation_bindings.binding_id"))
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    app_id: Mapped[str] = mapped_column(String(128), index=True)
    address: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 同一根只有一张任务交互卡，通知目标同时保存其渠道回执。
    card_id: Mapped[str | None] = mapped_column(String(128))
    card_message_id: Mapped[str | None] = mapped_column(String(128))
    card_sequence: Mapped[int] = mapped_column(Integer, default=0)
    card_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationEventRow(Base):
    """状态提交事务冻结的事件；逐行消费标记避免全局递增游标跳过迟提交事务。"""

    __tablename__ = "notification_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("notification_targets.target_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_notification_events_pending", "materialized_at", "created_at"),)


class NotificationDeliveryRow(Base):
    """固定分片、发送键和 epoch；sending 崩溃即为 uncertain，不能当作未发送。"""

    __tablename__ = "notification_deliveries"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("notification_events.event_id"))
    part: Mapped[int] = mapped_column(Integer)
    parts: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(String(16), default="text")
    card_id: Mapped[str | None] = mapped_column(String(128))
    target_message_id: Mapped[str | None] = mapped_column(String(128))
    send_key: Mapped[str] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    owner: Mapped[str | None] = mapped_column(String(128))
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recover_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_evidence_hash: Mapped[str | None] = mapped_column(String(64))
    message_id: Mapped[str | None] = mapped_column(String(128))
    error_class: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("uq_notification_delivery_part", "event_id", "part", unique=True),
        Index("ix_notification_deliveries_due", "due_at", "lease_until"),
    )


class NotificationSenderRow(Base):
    """发送角色心跳只证明进程存活，业务回执单独统计。"""

    __tablename__ = "notification_senders"
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
