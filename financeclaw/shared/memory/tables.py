"""Four PostgreSQL memory authority tables; Store contains derived indexes only."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base, utcnow

DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class MemoryOwnerRow(Base):
    """Serialize one owner's writes, revisions, privacy policy and consolidation wakeup."""

    __tablename__ = "memory_owners"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    memory_revision: Mapped[int] = mapped_column(Integer, default=0)
    source_seq: Mapped[int] = mapped_column(Integer, default=0)
    extraction_revision: Mapped[int] = mapped_column(Integer, default=0)
    consolidated_revision: Mapped[int] = mapped_column(Integer, default=0)
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    privacy_epoch: Mapped[int] = mapped_column(Integer, default=0)
    read_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    active_consolidation_event_id: Mapped[str | None] = mapped_column(String(128))
    digest: Mapped[dict[str, Any]] = mapped_column(DOCUMENT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemorySourceRow(Base):
    """Reference/hash catalog with separate visibility, derive permit and replay blocking."""

    __tablename__ = "memory_sources"
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    source_seq: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[str] = mapped_column(String(256))
    source_version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    turn_id: Mapped[str | None] = mapped_column(String(128))
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    version_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    reuse_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    permit: Mapped[dict[str, Any] | None] = mapped_column(DOCUMENT)
    permit_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    data_classification: Mapped[str] = mapped_column(String(32), default="internal")
    processing_region: Mapped[str] = mapped_column(String(64), default="global")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"], ["memory_owners.tenant_id", "memory_owners.subject_id"]
        ),
        UniqueConstraint("tenant_id", "subject_id", "source_seq", name="uq_memory_source_sequence"),
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "source_kind",
            "object_id",
            "source_version",
            name="uq_memory_source_object",
        ),
        UniqueConstraint("tenant_id", "subject_id", "source_id", name="uq_memory_source_owner"),
        Index("ix_memory_source_turn", "tenant_id", "subject_id", "turn_id"),
    )


class MemoryExtractionRow(Base):
    """A partition output and its exact readiness/disposition, never another job queue."""

    __tablename__ = "memory_extractions"
    extraction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    closure_hash: Mapped[str] = mapped_column(String(64))
    prepare_event_id: Mapped[str] = mapped_column(String(128))
    part_index: Mapped[int] = mapped_column(Integer)
    part_count: Mapped[int] = mapped_column(Integer)
    pipeline_version: Mapped[str] = mapped_column(String(64))
    model_profile_version: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[str] = mapped_column(String(64), default="memory-v1")
    evidence_source_ids: Mapped[list[str]] = mapped_column(DOCUMENT, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(DOCUMENT, default=list)
    output: Mapped[dict[str, Any]] = mapped_column(DOCUMENT, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(DOCUMENT, default=dict)
    extraction_revision: Mapped[int | None] = mapped_column(Integer)
    disposition: Mapped[str] = mapped_column(String(24), default="pending")
    disposition_reason: Mapped[str | None] = mapped_column(String(128))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"], ["memory_owners.tenant_id", "memory_owners.subject_id"]
        ),
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "closure_hash",
            "part_index",
            "pipeline_version",
            name="uq_memory_extraction_part",
        ),
        Index(
            "ix_memory_extraction_ready",
            "tenant_id",
            "subject_id",
            "disposition",
            "extraction_revision",
        ),
        Index("ix_memory_extraction_sources", "evidence_source_ids", postgresql_using="gin"),
    )


class MemoryRecordRow(Base):
    """Immutable fact revisions plus one current head, candidates and forget tombstones."""

    __tablename__ = "memory_records"
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(128))
    owner_revision: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    scope_type: Mapped[str] = mapped_column(String(24), default="user")
    scope_id: Mapped[str] = mapped_column(String(128), default="")
    field: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    evidence_source_ids: Mapped[list[str]] = mapped_column(DOCUMENT, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(DOCUMENT, default=list)
    source_watermark: Mapped[int] = mapped_column(Integer, default=0)
    mutation_id: Mapped[str] = mapped_column(String(256))
    operation: Mapped[str] = mapped_column(String(16), default="create")
    target_memory_id: Mapped[str | None] = mapped_column(String(128))
    expected_target_revision: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forgotten_through_seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"], ["memory_owners.tenant_id", "memory_owners.subject_id"]
        ),
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "memory_id",
            "revision",
            name="uq_memory_record_owner_version",
        ),
        Index(
            "uq_memory_current",
            "tenant_id",
            "subject_id",
            "memory_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "uq_memory_active_profile",
            "tenant_id",
            "subject_id",
            "scope_type",
            "scope_id",
            "field",
            unique=True,
            postgresql_where=text("is_current AND status = 'active' AND kind = 'profile'"),
            sqlite_where=text("is_current = 1 AND status = 'active' AND kind = 'profile'"),
        ),
        Index("ix_memory_record_sources", "evidence_source_ids", postgresql_using="gin"),
        Index("ix_memory_record_owner_status", "tenant_id", "subject_id", "is_current", "status"),
    )
