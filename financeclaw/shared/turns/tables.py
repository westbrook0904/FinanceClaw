"""Three durable entities: the business Turn, sending commands and human decisions."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from financeclaw.shared.infrastructure.orm import Base, utcnow

DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
OPEN_TURN = "status NOT IN ('completed', 'failed', 'cancelled')"


class ConversationTurnRow(Base):
    """One accepted user request, its immutable release and mutable business responsibility."""

    __tablename__ = "conversation_turns"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    user_message_id: Mapped[str] = mapped_column(String(128))
    thread_id: Mapped[str] = mapped_column(String(128))
    release_snapshot: Mapped[dict[str, Any]] = mapped_column(DOCUMENT)
    release_hash: Mapped[str] = mapped_column(String(64))
    current_command_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    status_reason: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    grant_scopes: Mapped[list[str]] = mapped_column(DOCUMENT)
    grant_source: Mapped[str] = mapped_column(String(32))
    grant_source_hash: Mapped[str] = mapped_column(String(64))
    grant_issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grant_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grant_revision: Mapped[int] = mapped_column(Integer, default=1)
    grant_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    command_calls: Mapped[int] = mapped_column(Integer, default=0)
    side_effects_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Any] = relationship(
        "ConversationRow", back_populates="turns", foreign_keys=[conversation_id]
    )
    messages: Mapped[list[Any]] = relationship(
        "ConversationMessageRow",
        back_populates="turn",
        cascade="all, delete-orphan",
        foreign_keys="ConversationMessageRow.turn_id",
    )
    __table_args__ = (
        UniqueConstraint("turn_id", "conversation_id", name="uq_turn_conversation"),
        UniqueConstraint("tenant_id", "subject_id", "idempotency_key", name="uq_turn_idempotency"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "subject_id"],
            [
                "conversations.conversation_id",
                "conversations.tenant_id",
                "conversations.subject_id",
            ],
            name="fk_turn_owner",
        ),
        ForeignKeyConstraint(
            ["turn_id", "user_message_id"],
            ["conversation_messages.turn_id", "conversation_messages.message_id"],
            name="fk_turn_user_message",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["turn_id", "current_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_turn_current_command",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "status IN ('accepted','queued','running','waiting','resuming',"
            "'cancelling','blocked','completed','failed','cancelled')",
            name="ck_turn_status",
        ),
        CheckConstraint(
            "model_calls >= 0 AND tool_calls >= 0 AND command_calls >= 0",
            name="ck_turn_budget_nonnegative",
        ),
        Index(
            "uq_turn_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(OPEN_TURN),
            sqlite_where=text(OPEN_TURN),
        ),
        Index(
            "ix_turn_due",
            "next_action_at",
            postgresql_where=text(OPEN_TURN),
            sqlite_where=text(OPEN_TURN),
        ),
        Index("ix_turn_conversation_created", "conversation_id", "created_at"),
    )


class TurnCommandRow(Base):
    """A fixed start/resume intent and the sole native execution receipt it may bind."""

    __tablename__ = "turn_commands"
    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(128), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict[str, Any]] = mapped_column(DOCUMENT)
    grant_revision: Mapped[int] = mapped_column(Integer)
    authorized_scopes: Mapped[list[str]] = mapped_column(DOCUMENT)
    state: Mapped[str] = mapped_column(String(24), default="prepared")
    native_run_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    receipt_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_checkpoint: Mapped[dict[str, Any] | None] = mapped_column(DOCUMENT)
    observation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("turn_id", "command_id", name="uq_command_turn"),
        UniqueConstraint("turn_id", "sequence", name="uq_command_sequence"),
        ForeignKeyConstraint(["turn_id"], ["conversation_turns.turn_id"], name="fk_command_turn"),
        CheckConstraint("kind IN ('start','resume')", name="ck_command_kind"),
        CheckConstraint(
            "state IN ('prepared','sending','uncertain','submitted','observed',"
            "'cancelled','rejected')",
            name="ck_command_state",
        ),
        Index(
            "uq_command_start",
            "turn_id",
            unique=True,
            postgresql_where=text("kind = 'start'"),
            sqlite_where=text("kind = 'start'"),
        ),
    )


class InteractionRow(Base):
    """Immutable question binding and a single user decision, separate from delivery."""

    __tablename__ = "interactions"
    interaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(128), index=True)
    origin_command_id: Mapped[str] = mapped_column(String(128))
    native_run_id: Mapped[str] = mapped_column(String(128))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(DOCUMENT)
    interrupt_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    request: Mapped[dict[str, Any]] = mapped_column(DOCUMENT)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    response_key: Mapped[str | None] = mapped_column(String(256))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[dict[str, Any] | None] = mapped_column(DOCUMENT)
    resume_command_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["turn_id"], ["conversation_turns.turn_id"], name="fk_interaction_turn"
        ),
        ForeignKeyConstraint(
            ["turn_id", "origin_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_interaction_origin",
        ),
        ForeignKeyConstraint(
            ["turn_id", "resume_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_interaction_resume",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("origin_command_id", "interrupt_id", name="uq_interaction_native"),
        UniqueConstraint("turn_id", "revision", name="uq_interaction_revision"),
        CheckConstraint(
            "status IN ('pending','resolved','rejected','expired','cancelled','superseded')",
            name="ck_interaction_status",
        ),
        Index(
            "uq_interaction_pending",
            "turn_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )
