"""``outbox_events`` 表的 SQLAlchemy ORM 映射，承载事务性发件箱事件。

业务写路径（如审计落库）在同一事务中插入本表，保证"永久 Audit 与有界
Outbox 事件"原子落盘；异步 publisher 通过本表领取并推进事件投递状态。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class OutboxEventRow(Base):
    """Outbox 事件的持久化行，对应 ``outbox_events`` 表。

    使用场景：与 Audit 行在同一事务中插入，实现 Transactional Outbox；publisher
    借助投递索引扫描到期事件并加租约，投递结果（成功、退避、死信）回写到
    本行的状态字段。

    Attributes:
        event_id: 事件唯一标识，主键，最长 128 字符。
        event_type: 事件类型字符串，最长 128 字符。
        aggregate_type: 聚合类型（如资源类型），最长 64 字符。
        aggregate_id: 聚合标识（如资源 ID），最长 128 字符。
        tenant_id: 租户 ID，最长 128 字符。
        subject_id: 主体（用户）ID，最长 128 字符。
        payload: 事件载荷字典，JSON 存储，默认空字典。
        status: 投递状态字符串，取值见 OutboxStatus，默认 ``pending``。
        attempts: 已尝试投递次数，默认 0。
        available_at: 何时可被领取（带时区），重试时按指数退避向后推移。
        locked_until: 租约到期时间（带时区），未持租约为 None。
        created_at: 创建时间（带时区 UTC），默认 utcnow。
        published_at: 投递成功时间，未投递为 None。
        last_error: 最近一次投递失败原因文本，无失败为 None。

    """

    __tablename__ = "outbox_events"
    # 投递索引支撑 publisher 按（状态，可用时间，租约时间）领取到期事件；
    # 归属索引支撑按租户与主体回溯事件创建记录。
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
