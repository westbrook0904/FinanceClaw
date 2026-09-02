"""Trusted execution context propagated outside model-visible messages."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class ExecutionContext(BaseModel):
    """Values derived from authenticated server-side identity and request state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: Identifier
    subject_id: Identifier
    scopes: frozenset[str] = Field(default_factory=frozenset)
    conversation_id: Identifier | None = None
    turn_id: Identifier
    run_id: Identifier
    data_classification: DataClassification = DataClassification.INTERNAL
    locale: Annotated[str, Field(min_length=2, max_length=32)] = "zh-CN"
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "Asia/Shanghai"

    def trace_metadata(self) -> dict[str, str]:
        """Return bounded metadata without exposing tenant or subject identifiers."""

        from hashlib import sha256

        def digest(value: str) -> str:
            return sha256(value.encode()).hexdigest()[:16]

        return {
            "tenant_hash": digest(self.tenant_id),
            "subject_hash": digest(self.subject_id),
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "data_classification": self.data_classification.value,
        }
