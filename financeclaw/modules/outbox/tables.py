"""声明 Outbox 事件的 SQLAlchemy 持久化映射。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class OutboxEventRow(Base):
    """定义Outbox事件Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        event_id: 审计或 Outbox 事件的稳定标识。
        event_type: 事件的语义类型，供消费者选择处理逻辑。
        aggregate_type: 产生 Outbox 事件的聚合根类别。
        aggregate_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        payload: 事件携带的结构化业务数据。
        status: 当前生命周期状态，决定记录允许的后续操作。
        attempts: 已经尝试投递或执行的次数。
        available_at: 该生命周期事件发生的 UTC 时间。
        locked_until: 事件领取租约的到期时间，防止多个发布者重复处理。
        created_at: 记录创建时间，统一按 UTC 解释。
        published_at: 该生命周期事件发生的 UTC 时间。
        last_error: 最近一次失败原因，尚未失败时为空。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_delivery", "status", "available_at", "locked_until"),
        Index("ix_outbox_owner", "tenant_id", "subject_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
