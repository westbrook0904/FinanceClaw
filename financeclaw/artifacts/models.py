"""Artifact metadata stored separately from large content."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    tenant_id: str
    subject_id: str
    content_type: str
    storage_uri: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    source_type: str
    source_id: str
    access_policy: dict[str, Any] = Field(default_factory=dict)
    encryption_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
