"""SQLAlchemy row for durable parent-child run mappings."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class DelegationRow(Base):
    __tablename__ = "delegations"
    __table_args__ = (
        UniqueConstraint("child_run_id", name="uq_delegations_child_run"),
        Index(
            "ix_delegations_parent",
            "tenant_id",
            "subject_id",
            "parent_run_id",
            "created_at",
        ),
        Index("ix_delegations_incomplete", "status", "updated_at"),
    )

    delegation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    parent_turn_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_turns.turn_id"), nullable=False
    )
    parent_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(String(128))
    child_thread_id: Mapped[str | None] = mapped_column(String(128))
    child_server_run_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
