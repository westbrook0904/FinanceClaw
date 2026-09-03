"""Published-workflow definitions and durable business records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenWorkflowModel(BaseModel):
    """Strict immutable base for persisted workflow facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class WorkflowRunStatus(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class WorkflowApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WorkflowToolRef(FrozenWorkflowModel):
    tool_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ApprovalPoint(FrozenWorkflowModel):
    """One published interrupt contract; it cannot be added at runtime."""

    approval_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    requested_action: str = Field(min_length=1, max_length=128)
    allowed_decisions: tuple[Literal["approve", "reject"], ...] = ("approve", "reject")
    required_scope: str = "workflows:approve"

    @model_validator(mode="after")
    def decisions_are_nonempty_and_unique(self) -> ApprovalPoint:
        if not self.allowed_decisions or len(self.allowed_decisions) != len(
            set(self.allowed_decisions)
        ):
            raise ValueError("approval decisions must be nonempty and unique")
        return self


class WorkflowTimeoutPolicy(FrozenWorkflowModel):
    run_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    approval_timeout_seconds: int = Field(default=900, ge=30, le=604_800)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Immutable startup entry binding a business version to one compiled graph."""

    workflow_id: str
    version: str
    assistant_id: str
    graph: Any
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    model_profile_id: str
    allowed_tools: tuple[WorkflowToolRef, ...]
    approval_points: tuple[ApprovalPoint, ...]
    timeout_policy: WorkflowTimeoutPolicy
    status: WorkflowStatus
    deployment_revision: str
    required_scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.workflow_id or not self.assistant_id or not self.deployment_revision:
            raise ValueError("workflow identifiers cannot be empty")
        parts = self.version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("workflow version must use semantic x.y.z form")
        if self.graph is None:
            raise ValueError("published workflow requires a compiled graph")
        tool_keys = tuple((item.tool_id, item.version) for item in self.allowed_tools)
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("workflow allowed tool versions must be unique")
        approval_ids = tuple(item.approval_id for item in self.approval_points)
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("workflow approval point IDs must be unique")

    @property
    def key(self) -> tuple[str, str]:
        return self.workflow_id, self.version

    def normalize_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate at the BFF boundary before a run reaches Agent Server."""

        return self.input_schema.model_validate(arguments).model_dump(mode="json")


class WorkflowRun(FrozenWorkflowModel):
    run_id: str
    tenant_id: str
    subject_id: str
    workflow_id: str
    workflow_version: str
    assistant_id: str
    deployment_revision: str
    model_profile_id: str
    run_timeout_seconds: int = Field(ge=1)
    approval_timeout_seconds: int = Field(ge=1)
    thread_id: str
    server_run_id: str | None = None
    client_idempotency_key: str
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None = None
    artifact_refs: tuple[str, ...] = ()
    status: WorkflowRunStatus
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class WorkflowApproval(FrozenWorkflowModel):
    approval_id: str
    run_id: str
    tenant_id: str
    subject_id: str
    approval_point: str
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_action: str
    request_payload: dict[str, Any]
    allowed_decisions: tuple[str, ...]
    required_scope: str
    status: WorkflowApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
