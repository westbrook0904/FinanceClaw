"""Harness Policy 的稳定上下文与结构化决策。"""

from __future__ import annotations

from enum import StrEnum

from harness_contracts import (
    ApprovalGrant,
    CapabilityDescriptor,
    ContractModel,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    ProviderDescriptor,
)
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from pydantic import Field, model_validator


class PolicyPhase(StrEnum):
    """Harness 当前支持的治理边界。"""

    PRE_ROUTE = "pre_route"
    PRE_PLAN = "pre_plan"
    PRE_EXECUTE = "pre_execute"


class PolicyEffect(StrEnum):
    """Policy 允许的三种治理结果。"""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyContext(ContractModel):
    """Policy 评估所需的只读上下文。"""

    invocation: InvocationContext
    phase: PolicyPhase = PolicyPhase.PRE_EXECUTE
    capability: CapabilityDescriptor | None = None
    provider: ProviderDescriptor | None = None
    plan: ExecutionPlan | None = None
    approval_grant: ApprovalGrant | None = None
    requested_mode: ExecutionMode | None = None

    @model_validator(mode="after")
    def validate_phase_payload(self) -> PolicyContext:
        if self.phase is PolicyPhase.PRE_ROUTE:
            if self.requested_mode is None:
                raise ValueError("pre_route policy context requires requested_mode")
            if (
                self.plan is not None
                or self.capability is not None
                or self.provider is not None
                or self.approval_grant is not None
            ):
                raise ValueError(
                    "pre_route policy context forbids plan, capability, provider and approval_grant"
                )
            return self

        if self.requested_mode is not None:
            raise ValueError("requested_mode is only valid for pre_route policy context")
        if self.phase is PolicyPhase.PRE_PLAN:
            if self.plan is None:
                raise ValueError("pre_plan policy context requires plan")
            if (
                self.capability is not None
                or self.provider is not None
                or self.approval_grant is not None
            ):
                raise ValueError(
                    "pre_plan policy context forbids capability, provider and approval_grant"
                )
            return self

        if self.capability is None:
            raise ValueError("pre_execute policy context requires capability")
        if self.plan is not None:
            raise ValueError("pre_execute policy context forbids plan")
        if (
            self.provider is not None
            and self.provider.capability_id != self.capability.id
        ):
            raise ValueError("provider capability_id must match capability.id")
        return self


class PolicyDecision(ContractModel):
    """单个 Policy 或 PolicyEngine 返回的结构化决策。"""

    effect: PolicyEffect
    policy: NonEmptyString
    reason: NonEmptyString | None = None
    constraints: FrozenJsonMapping = Field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        policy: str,
        *,
        reason: str | None = None,
        constraints: dict | None = None,
    ) -> PolicyDecision:
        return cls(
            effect=PolicyEffect.ALLOW,
            policy=policy,
            reason=reason,
            constraints=constraints or {},
        )

    @classmethod
    def deny(
        cls,
        policy: str,
        *,
        reason: str,
        constraints: dict | None = None,
    ) -> PolicyDecision:
        return cls(
            effect=PolicyEffect.DENY,
            policy=policy,
            reason=reason,
            constraints=constraints or {},
        )

    @classmethod
    def require_approval(
        cls,
        policy: str,
        *,
        reason: str,
        constraints: dict | None = None,
    ) -> PolicyDecision:
        return cls(
            effect=PolicyEffect.REQUIRE_APPROVAL,
            policy=policy,
            reason=reason,
            constraints=constraints or {},
        )
