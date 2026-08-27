"""Provider 候选选择所使用的稳定输入与输出协议。"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, FrozenJsonMapping, NonEmptyString
from .capability import EgressType, SideEffectType
from .context import _require_timezone
from .provider import ProviderPin


class SelectionContext(ContractModel):
    """一次 Provider Selection 的业务无关、最小可信上下文。"""

    request_id: NonEmptyString
    capability_id: NonEmptyString
    tenant_id: NonEmptyString | None = None
    identity_subject: NonEmptyString | None = None
    side_effect: SideEffectType
    egress: EgressType
    deadline_at: datetime | None = None
    provider_pin: ProviderPin | None = None
    canary_subject: NonEmptyString | None = None
    policy_constraints: FrozenJsonMapping = Field(default_factory=dict)
    attributes: FrozenJsonMapping = Field(default_factory=dict)

    _validate_deadline_at = field_validator("deadline_at")(_require_timezone)


class SelectionRejection(ContractModel):
    """候选 Provider 被 Eligibility 拒绝的结构化原因。"""

    provider_id: NonEmptyString
    reason_code: NonEmptyString
    details: FrozenJsonMapping = Field(default_factory=dict)


class SelectionDecision(ContractModel):
    """成功 Selection 的稳定审计结果。"""

    capability_id: NonEmptyString
    selected_provider_id: NonEmptyString
    eligible_candidates: tuple[NonEmptyString, ...] = Field(min_length=1)
    rejected_candidates: tuple[SelectionRejection, ...] = ()
    selector: NonEmptyString
    reason_code: NonEmptyString
    selection_key: NonEmptyString

    @model_validator(mode="after")
    def validate_candidate_sets(self) -> Self:
        eligible_ids = tuple(self.eligible_candidates)
        if len(set(eligible_ids)) != len(eligible_ids):
            raise ValueError("eligible_candidates must not contain duplicate provider ids")

        rejected_ids = tuple(item.provider_id for item in self.rejected_candidates)
        if len(set(rejected_ids)) != len(rejected_ids):
            raise ValueError("rejected_candidates must not contain duplicate provider ids")

        if self.selected_provider_id not in eligible_ids:
            raise ValueError("selected_provider_id must be included in eligible_candidates")

        overlap = set(eligible_ids).intersection(rejected_ids)
        if overlap:
            raise ValueError("provider ids cannot be both eligible and rejected")
        return self
