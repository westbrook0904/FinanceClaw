"""交互决定与恢复操作分开记录：决定终态不等于远程执行完成。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base


class PendingInteractionRow(Base):
    """每个原生中断实例一条记录，根任务树最多一个 pending 用户交互。"""

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
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    response_key: Mapped[str | None] = mapped_column(String(256))
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
