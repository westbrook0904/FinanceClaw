"""Human approval 的持久化安全协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator

from .base import ContractModel, FrozenJsonMapping, NonEmptyString
from .capability import EgressType, SideEffectType
from .context import _require_timezone


class ApprovalDecisionType(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(ContractModel):
    approval_id: NonEmptyString
    plan_id: NonEmptyString
    node_id: NonEmptyString
    capability: NonEmptyString | None = None
    resource_category: NonEmptyString | None = None
    side_effect: SideEffectType = SideEffectType.NONE
    egress: EgressType = EgressType.NONE
    parameter_summary: FrozenJsonMapping = Field(default_factory=dict)
    reason: NonEmptyString
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    _validate_requested_at = field_validator("requested_at")(_require_timezone)


class ApprovalDecision(ContractModel):
    approval_id: NonEmptyString
    decision: ApprovalDecisionType
    decided_by: NonEmptyString
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: NonEmptyString | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    _validate_decided_at = field_validator("decided_at")(_require_timezone)
