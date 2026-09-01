"""Agent Foundation F4a 的最小 Exploration 公共契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    FrozenJsonValue,
    MutableContractModel,
    NonEmptyString,
)
from .context import _require_timezone
from .context_engineering import ContextUseRecord
from .request import RequestInput
from .result import ResultEnvelope, ResultOutput, ResultStatus

ExplorationHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExplorationBudget(ContractModel):
    max_steps: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    max_action_calls: int = Field(ge=0)
    max_repeated_actions: int = Field(ge=0)
    max_observations: int = Field(ge=0)


class ExplorationUsage(MutableContractModel):
    steps: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    action_calls: int = Field(default=0, ge=0)


class ExplorationProfile(ContractModel):
    profile_id: NonEmptyString
    model_capability_id: NonEmptyString
    allowed_capability_ids: frozenset[NonEmptyString] = Field(min_length=1)
    default_budget: ExplorationBudget
    prompt_version: NonEmptyString
    memory_required: bool = False


class ExplorationProfileSnapshot(ContractModel):
    profile_id: NonEmptyString
    model_capability_id: NonEmptyString
    allowed_capability_ids: frozenset[NonEmptyString] = Field(min_length=1)
    budget: ExplorationBudget
    prompt_version: NonEmptyString
    memory_required: bool
    profile_hash: ExplorationHash


class CallCapabilityDraft(ContractModel):
    kind: Literal["call_capability"] = "call_capability"
    capability_id: NonEmptyString
    input: RequestInput
    reason_code: NonEmptyString


class FinishDraft(ContractModel):
    kind: Literal["finish"] = "finish"
    output: ResultOutput
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)
    reason_code: NonEmptyString

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("finish evidence_refs must be unique")
        return value


ExplorationTurnDraft = Annotated[
    CallCapabilityDraft | FinishDraft,
    Field(discriminator="kind"),
]


class ExplorationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class ActionProposal(ContractModel):
    action_id: NonEmptyString
    exploration_id: NonEmptyString
    step: int = Field(ge=1)
    capability_id: NonEmptyString
    input: RequestInput
    proposal_hash: ExplorationHash
    catalog_snapshot_hash: ExplorationHash
    scope_hash: ExplorationHash
    context_projection_hash: ExplorationHash
    reason_code: NonEmptyString


type ActionExecutionStatus = Literal[
    "proposed",
    "running",
    "succeeded",
    "failed",
    "denied",
    "cancelled",
    "orphaned",
]


class ActionExecutionState(MutableContractModel):
    action_id: NonEmptyString
    status: ActionExecutionStatus = "proposed"
    proposal: ActionProposal
    result: ResultEnvelope | None = None
    error_code: NonEmptyString | None = None
    observation_id: NonEmptyString | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.action_id != self.proposal.action_id:
            raise ValueError("action state identity must match proposal")
        return self


class Observation(ContractModel):
    observation_id: NonEmptyString
    action_id: NonEmptyString
    result_status: ResultStatus
    bounded_summary: FrozenJsonValue
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)
    result_hash: ExplorationHash

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("observation evidence_refs must be unique")
        return value


class ExplorationState(MutableContractModel):
    exploration_id: NonEmptyString
    plan_id: NonEmptyString
    node_id: NonEmptyString
    profile: ExplorationProfileSnapshot
    status: ExplorationStatus = ExplorationStatus.CREATED
    usage: ExplorationUsage = Field(default_factory=ExplorationUsage)
    scope_hash: ExplorationHash
    context_uses: list[ContextUseRecord] = Field(default_factory=list)
    actions: list[ActionExecutionState] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    pending_action_id: NonEmptyString | None = None
    final_result: ResultEnvelope | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)

    @model_validator(mode="after")
    def validate_local_identity_sets(self) -> Self:
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("exploration action IDs must be unique")
        observation_ids = [observation.observation_id for observation in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("exploration observation IDs must be unique")
        if len(self.observations) > self.profile.budget.max_observations:
            raise ValueError("exploration observations exceed persisted budget")
        return self
