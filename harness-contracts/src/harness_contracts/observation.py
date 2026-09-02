"""供 Agent 上下文消费的有界工具调用 Observation。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator

from .base import ContractModel, FrozenJsonValue, NonEmptyString

ObservationHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ObservationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Observation(ContractModel):
    observation_id: NonEmptyString
    action_id: NonEmptyString
    result_status: ObservationStatus
    bounded_summary: FrozenJsonValue
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)
    result_hash: ObservationHash

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("observation evidence_refs must be unique")
        return value
