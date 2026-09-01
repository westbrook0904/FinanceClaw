"""计划与节点的可恢复执行状态协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator

from .approval import ApprovalRequest
from .base import JsonValue, MutableContractModel, NonEmptyString
from .context import _require_timezone
from .errors import ErrorDetail
from .exploration import ExplorationState
from .provider import ProviderAttempt
from .result import Continuation, ResultEnvelope, ResultIssue


class PlanExecutionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class NodeExecutionStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class NodeExecutionState(MutableContractModel):
    node_id: NonEmptyString
    status: NodeExecutionStatus = NodeExecutionStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    selected_provider_id: NonEmptyString | None = None
    provider_attempt: int = Field(default=0, ge=0)
    provider_retry_attempt: int = Field(default=0, ge=0)
    provider_selection_key: NonEmptyString | None = None
    provider_equivalence_group: NonEmptyString | None = None
    provider_history: list[ProviderAttempt] = Field(default_factory=list)
    provider_last_result: ResultEnvelope | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: ResultEnvelope | None = None
    error: ErrorDetail | None = None
    waiting_reason: str | None = None
    continuation: Continuation | None = None

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)


class PlanExecutionState(MutableContractModel):
    plan_id: NonEmptyString
    plan_revision: int = Field(ge=1)
    state_version: int = Field(default=1, ge=1)
    status: PlanExecutionStatus = PlanExecutionStatus.CREATED
    nodes: dict[str, NodeExecutionState] = Field(default_factory=dict)
    explorations: dict[str, ExplorationState] = Field(default_factory=dict)
    issues: list[ResultIssue] = Field(default_factory=list)
    pending_approvals: list[ApprovalRequest] = Field(default_factory=list)
    pending_jobs: list[Continuation] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)
