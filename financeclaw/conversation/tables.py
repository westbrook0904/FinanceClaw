"""SQLAlchemy tables owned by the FinanceClaw application database."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner", "tenant_id", "subject_id", "conversation_id"),
        UniqueConstraint("agent_thread_id", name="uq_conversations_agent_thread"),
    )

    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    turns: Mapped[list["ConversationTurnRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class ConversationTurnRow(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_conversation_turns_run"),
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "client_idempotency_key",
            name="uq_conversation_turns_owner_idempotency",
        ),
        Index("ix_conversation_turns_conversation_created", "conversation_id", "created_at"),
    )

    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    client_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[ConversationRow] = relationship(back_populates="turns")
    messages: Mapped[list["ConversationMessageRow"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_sequence"),
        Index("ix_messages_turn_role", "turn_id", "role"),
    )

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id")
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    turn: Mapped[ConversationTurnRow] = relationship(back_populates="messages")


class ConversationSummaryRow(Base):
    __tablename__ = "conversation_summaries"
    __table_args__ = (
        Index(
            "ix_summaries_conversation_range",
            "conversation_id",
            "level",
            "start_sequence",
            "end_sequence",
        ),
    )

    summary_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_summary_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    summary_content: Mapped[str] = mapped_column(Text, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    entities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    open_items: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    model_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    superseded_by: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_summaries.summary_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ModelContextManifestRow(Base):
    __tablename__ = "model_context_manifests"
    __table_args__ = (
        UniqueConstraint("model_call_id", name="uq_manifests_model_call"),
        Index("ix_manifests_run", "conversation_id", "turn_id", "run_id"),
    )

    manifest_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    recent_message_start: Mapped[int | None] = mapped_column(Integer)
    recent_message_end: Mapped[int | None] = mapped_column(Integer)
    summary_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    historical_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_result_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exposed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    omissions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ArtifactMetadataRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_owner", "tenant_id", "subject_id", "artifact_id"),)

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    access_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    encryption_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
