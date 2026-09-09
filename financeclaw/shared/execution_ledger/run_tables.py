"""BFF 根运行、授权、通知唤醒与后端尝试的共享存储声明。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base, utcnow


class RootRunRow(Base):
    """引用既有业务根，保存可重建进度及唯一到期责任，不复制消息或图状态。"""

    __tablename__ = "root_runs"
    run_id: Mapped[str] = mapped_column(ForeignKey("run_executions.run_id"), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"))
    backend_instance_id: Mapped[str] = mapped_column(String(128))
    driver_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    projection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    wake: Mapped[int] = mapped_column(Integer, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("ix_root_due", "backend_instance_id", "active", "due_at"),
        Index("ix_bff_driver_due", "driver_version", "backend_instance_id", "active", "due_at"),
        Index(
            "uq_root_active_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active = true"),
        ),
    )


class RunAuthorizationRow(Base):
    """有限期授权与可信来源摘要；不保存 bearer token，也不改变原执行快照。"""

    __tablename__ = "run_authorizations"
    run_id: Mapped[str] = mapped_column(ForeignKey("run_executions.run_id"), primary_key=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32))
    source_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class RunInboxRow(Base):
    """命令引用与最小回调分型保存；早到通知也有持久化关联和清理责任。"""

    __tablename__ = "run_inbox"
    inbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("root_runs.run_id"))
    backend_instance_id: Mapped[str] = mapped_column(String(128))
    execution_hash: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_run_inbox_binding", "backend_instance_id", "execution_hash", "processed"),
        Index("ix_run_inbox_root", "run_id", "processed"),
        Index("ix_run_inbox_expiry", "expires_at"),
    )


class BackendAttemptRow(Base):
    """一次固定 operation 的确切 backend 引用，部署与 opaque ID 联合唯一。"""

    __tablename__ = "backend_attempts"
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("run_operations.operation_id"), primary_key=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("root_runs.run_id"), index=True)
    backend_instance_id: Mapped[str] = mapped_column(String(128))
    execution_hash: Mapped[str] = mapped_column(String(64))
    reference: Mapped[dict[str, Any]] = mapped_column(JSON)
    cancellation_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("uq_backend_attempt_identity", "backend_instance_id", "execution_hash", unique=True),
    )


class RunProgressEventRow(Base):
    """根与 revision 唯一的业务进度，不保存 token 或 backend 原始状态。"""

    __tablename__ = "run_progress_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("root_runs.run_id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
