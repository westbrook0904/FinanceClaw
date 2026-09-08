"""交互决定与恢复操作分开记录：决定终态不等于远程执行完成。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base


class PendingInteractionRow(Base):
    """保存一个原生中断对应的问题快照及其唯一决定。

    root_run_id 控制整棵任务树最多一个 pending 交互，owner_run_id 标识
    真正等待回答的业务运行。server_run_id 与 interrupt_id 共同定位恢复点，
    revision 在同一 owner 内递增，用于拒绝旧页面或旧消息的回答。

    resolved/rejected 表示决定已落盘；远程恢复是否已受理及完成，需要沿
    operation_id 查询 run_operations，不能从交互终态推断。
    """

    __tablename__ = "pending_interactions"
    interaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    root_run_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_run_id: Mapped[str] = mapped_column(ForeignKey("run_executions.run_id"), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(128))
    delegation_id: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32))
    thread_id: Mapped[str] = mapped_column(String(128))
    server_run_id: Mapped[str] = mapped_column(String(128))
    interrupt_id: Mapped[str] = mapped_column(String(128))
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    point_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    question: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    # pending 可结束为 resolved、rejected、expired、cancelled 或 superseded。
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    response_key: Mapped[str | None] = mapped_column(String(256))
    # 幂等键和内容摘要共同判断是否为原决定重放；同键不同回答必须冲突。
    response_hash: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    operation_id: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        Index(
            "uq_interactions_pending_root",
            "root_run_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )
