"""Agent Foundation F3 的长期 Memory 公共契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, FrozenJsonValue, NonEmptyString

MemoryHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MemoryKind(StrEnum):
    CONVERSATION = "conversation"
    PREFERENCE = "preference"
    DOMAIN_FACT = "domain_fact"


class MemorySensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


class MemorySubjectScope(ContractModel):
    tenant_id: NonEmptyString
    subject_id: NonEmptyString


class MemoryProvenance(ContractModel):
    producer: NonEmptyString
    source_fact_hash: MemoryHash
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("memory evidence_refs must be unique")
        return value


class MemoryRecord(ContractModel):
    memory_id: NonEmptyString
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespace: NonEmptyString
    kind: MemoryKind
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(default_factory=frozenset, max_length=16)
    sensitivity: MemorySensitivity
    provenance: MemoryProvenance
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_create_only_lifecycle(self) -> Self:
        if self.updated_at != self.created_at:
            raise ValueError("create-only memory requires updated_at == created_at")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("memory expires_at must be after created_at")
        return self


class MemoryQuery(ContractModel):
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespaces: frozenset[NonEmptyString] = Field(min_length=1, max_length=8)
    kinds: frozenset[MemoryKind] = Field(
        default_factory=lambda: frozenset(MemoryKind),
        min_length=1,
        max_length=3,
    )
    tags: frozenset[NonEmptyString] = Field(default_factory=frozenset, max_length=16)
    text: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("memory query text must not contain surrounding whitespace")
        return value


class MemorySlice(ContractModel):
    records: tuple[MemoryRecord, ...] = Field(max_length=50)
    query_hash: MemoryHash
    truncated: bool = False

    @model_validator(mode="after")
    def validate_unique_records(self) -> Self:
        memory_ids = [record.memory_id for record in self.records]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("memory slice record IDs must be unique")
        return self


class MemoryWriteDraft(ContractModel):
    kind: MemoryKind
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(default_factory=frozenset, max_length=16)
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("memory evidence_refs must be unique")
        return value


class MemoryWriteProposal(ContractModel):
    proposal_id: NonEmptyString
    proposal_hash: MemoryHash
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespace: NonEmptyString
    kind: MemoryKind
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(default_factory=frozenset, max_length=16)
    sensitivity: MemorySensitivity
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)
    source_fact_hash: MemoryHash
    provenance: MemoryProvenance
    expires_at: datetime | None = None

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("memory evidence_refs must be unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_provenance_alignment(self) -> Self:
        if self.provenance.source_fact_hash != self.source_fact_hash:
            raise ValueError("memory provenance source_fact_hash must match proposal")
        if self.provenance.evidence_refs != self.evidence_refs:
            raise ValueError("memory provenance evidence_refs must match proposal")
        return self


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value
