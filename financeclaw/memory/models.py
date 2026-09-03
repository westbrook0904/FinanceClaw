"""Stable domain records for governed cross-conversation memory."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenMemoryModel(BaseModel):
    """Reject unrecognized model output and prevent post-validation mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryType(StrEnum):
    """Long-lived user information that may safely cross conversations."""

    PREFERENCE = "preference"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    DECISION_NOTE = "decision_note"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    DELETED = "deleted"


class MemorySensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


MemoryIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class MemoryDraft(FrozenMemoryModel):
    """The complete and deliberately small model-controlled proposal shape.

    Tenant, subject, namespace, sensitivity and lifecycle fields are absent by
    design. A caller cannot smuggle those authoritative values through model
    output because ``extra='forbid'`` rejects every undeclared field.
    """

    kind: MemoryType
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    evidence_message_ids: tuple[MemoryIdentifier, ...] = Field(min_length=1, max_length=32)

    @field_validator("content")
    @classmethod
    def content_must_be_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("memory content must not contain surrounding whitespace")
        return value

    @field_validator("evidence_message_ids")
    @classmethod
    def evidence_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("memory evidence message IDs must be unique")
        return value


class MemoryProvenance(FrozenMemoryModel):
    """System-bound identity and run references for one committed record."""

    conversation_id: MemoryIdentifier
    turn_id: MemoryIdentifier
    run_id: MemoryIdentifier
    producer: str = "financeclaw.long_term_memory_service"


class MemoryProposal(FrozenMemoryModel):
    """Validated proposal returned to the model before the HITL write step."""

    proposal_id: MemoryIdentifier
    draft: MemoryDraft
    sensitivity: MemorySensitivity
    requires_confirmation: bool
    confirmation_reason: str
    policy_version: str


class MemoryRecord(FrozenMemoryModel):
    """Governed long-term record persisted as JSON in LangGraph Store."""

    memory_id: MemoryIdentifier
    tenant_id: MemoryIdentifier
    subject_id: MemoryIdentifier
    namespace: tuple[str, ...] = Field(min_length=5, max_length=5)
    memory_type: MemoryType
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_message_ids: tuple[MemoryIdentifier, ...] = Field(min_length=1, max_length=32)
    created_at: datetime
    updated_at: datetime
    supersedes_id: MemoryIdentifier | None = None
    sensitivity: MemorySensitivity
    provenance: MemoryProvenance
    valid_until: datetime | None = None
    schema_version: int = Field(default=1, ge=1)

    @field_validator("created_at", "updated_at", "valid_until")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("memory timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("memory updated_at cannot precede created_at")
        if self.valid_until is not None and self.valid_until <= self.created_at:
            raise ValueError("memory valid_until must be after created_at")
        if self.supersedes_id == self.memory_id:
            raise ValueError("memory cannot supersede itself")
        return self


class MemoryRecall(FrozenMemoryModel):
    """A record selected for the current model call with an explainable reason."""

    record: MemoryRecord
    reason: str
    score: float = Field(ge=0)
