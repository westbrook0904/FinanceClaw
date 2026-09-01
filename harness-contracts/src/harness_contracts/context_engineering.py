"""Agent Foundation 的不可变 Context Engineering 公共契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, FrozenJsonValue, NonEmptyString

ContextHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ContextSourceKind(StrEnum):
    SYSTEM_INSTRUCTION = "system_instruction"
    REQUEST = "request"
    SESSION = "session"
    MEMORY = "memory"
    CAPABILITY_CATALOG = "capability_catalog"
    OBSERVATION = "observation"


class ContextConsumer(StrEnum):
    ROUTE = "route"
    PLAN = "plan"
    EXPLORE = "explore"


class ContextTrustTier(StrEnum):
    SYSTEM = "system"
    APPLICATION = "application"
    USER = "user"
    DATA = "data"


class ContextSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


class ContextOmissionReason(StrEnum):
    CONSUMER_FILTER = "consumer_filter"
    ITEM_TOO_LARGE = "item_too_large"
    MAX_ITEMS = "max_items"
    MAX_CHARS = "max_chars"
    MAX_OBSERVATIONS = "max_observations"
    MAX_MEMORY_RECORDS = "max_memory_records"


class ContextSourceRef(ContractModel):
    source_kind: ContextSourceKind
    source_id: NonEmptyString


class ContextProvenance(ContractModel):
    producer: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple, max_length=16)


class ContextFreshness(ContractModel):
    source_version: NonEmptyString
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)


class ContextItem(ContractModel):
    item_id: NonEmptyString
    kind: NonEmptyString
    content: FrozenJsonValue
    source: ContextSourceRef
    provenance: ContextProvenance
    freshness: ContextFreshness
    trust_tier: ContextTrustTier
    sensitivity: ContextSensitivity
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("context item expires_at must be after created_at")
        return self


class ContextOmission(ContractModel):
    item_id: NonEmptyString
    reason: ContextOmissionReason


class ContextSnapshot(ContractModel):
    snapshot_id: NonEmptyString
    items: tuple[ContextItem, ...] = Field(max_length=256)
    canonical_hash: ContextHash
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @model_validator(mode="after")
    def validate_unique_items(self) -> Self:
        _require_unique_item_ids(self.items, "snapshot items")
        return self


class ContextProjection(ContractModel):
    consumer: ContextConsumer
    snapshot_id: NonEmptyString
    items: tuple[ContextItem, ...] = Field(max_length=128)
    omitted: tuple[ContextOmission, ...] = Field(default_factory=tuple, max_length=256)
    projection_hash: ContextHash

    @model_validator(mode="after")
    def validate_projection_membership(self) -> Self:
        included_ids = _require_unique_item_ids(self.items, "projection items")
        omitted_ids = [item.item_id for item in self.omitted]
        if len(omitted_ids) != len(set(omitted_ids)):
            raise ValueError("projection omitted item IDs must be unique")
        if included_ids.intersection(omitted_ids):
            raise ValueError("projection items and omissions must be disjoint")
        return self


class ContextUseRecord(ContractModel):
    use_id: NonEmptyString
    consumer: ContextConsumer
    snapshot_id: NonEmptyString
    snapshot_hash: ContextHash
    projection_hash: ContextHash
    included_item_ids: tuple[NonEmptyString, ...] = Field(max_length=128)
    omitted: tuple[ContextOmission, ...] = Field(default_factory=tuple, max_length=256)
    assembled_at: datetime

    @field_validator("assembled_at")
    @classmethod
    def validate_assembled_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        included_ids = list(self.included_item_ids)
        omitted_ids = [item.item_id for item in self.omitted]
        if len(included_ids) != len(set(included_ids)):
            raise ValueError("included context item IDs must be unique")
        if len(omitted_ids) != len(set(omitted_ids)):
            raise ValueError("omitted context item IDs must be unique")
        if set(included_ids).intersection(omitted_ids):
            raise ValueError("included and omitted context item IDs must be disjoint")
        return self


class ContextProjectionLimits(ContractModel):
    max_items: int = Field(default=64, ge=1, le=128)
    max_chars: int = Field(default=32_768, ge=1, le=262_144)
    max_chars_per_item: int = Field(default=8_192, ge=1, le=65_536)
    max_observations: int = Field(default=16, ge=0, le=128)
    max_memory_records: int = Field(default=20, ge=0, le=128)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value


def _require_unique_item_ids(
    items: tuple[ContextItem, ...],
    label: str,
) -> set[str]:
    item_ids = [item.item_id for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError(f"{label} must have unique item IDs")
    return set(item_ids)
