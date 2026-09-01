"""Harness Policy 的稳定上下文与结构化决策。"""

from __future__ import annotations

from enum import StrEnum

from harness_contracts import (
    ApprovalGrant,
    CapabilityDescriptor,
    ContextConsumer,
    ContextItem,
    ContractModel,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    MemoryQuery,
    MemoryRecord,
    MemorySubjectScope,
    MemoryWriteProposal,
    ProviderDescriptor,
)
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from pydantic import Field, model_validator


class PolicyPhase(StrEnum):
    """Harness 当前支持的治理边界。"""

    PRE_CONTEXT = "pre_context"
    PRE_MEMORY_READ = "pre_memory_read"
    PRE_MEMORY_WRITE = "pre_memory_write"
    PRE_MEMORY_DELETE = "pre_memory_delete"
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
    context_item: ContextItem | None = None
    context_consumer: ContextConsumer | None = None
    memory_scope: MemorySubjectScope | None = None
    memory_query: MemoryQuery | None = None
    memory_record: MemoryRecord | None = None
    memory_proposal: MemoryWriteProposal | None = None

    @model_validator(mode="after")
    def validate_phase_payload(self) -> PolicyContext:
        memory_phases = {
            PolicyPhase.PRE_MEMORY_READ,
            PolicyPhase.PRE_MEMORY_WRITE,
            PolicyPhase.PRE_MEMORY_DELETE,
        }
        if self.phase in memory_phases:
            return self._validate_memory_payload()

        if any(
            value is not None
            for value in (
                self.memory_scope,
                self.memory_query,
                self.memory_record,
                self.memory_proposal,
            )
        ):
            raise ValueError("memory fields are only valid for pre_memory phases")
        if self.phase is PolicyPhase.PRE_CONTEXT:
            if self.context_item is None or self.context_consumer is None:
                raise ValueError(
                    "pre_context policy context requires context_item and context_consumer"
                )
            if (
                self.requested_mode is not None
                or self.plan is not None
                or self.capability is not None
                or self.provider is not None
                or self.approval_grant is not None
            ):
                raise ValueError(
                    "pre_context policy context forbids route, plan, capability, "
                    "provider and approval fields"
                )
            return self

        if self.context_item is not None or self.context_consumer is not None:
            raise ValueError("context_item and context_consumer are only valid for pre_context")
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
        if self.provider is not None and self.provider.capability_id != self.capability.id:
            raise ValueError("provider capability_id must match capability.id")
        return self

    def _validate_memory_payload(self) -> PolicyContext:
        if self.memory_scope is None:
            raise ValueError("pre_memory policy context requires memory_scope")
        if any(
            value is not None
            for value in (
                self.requested_mode,
                self.plan,
                self.capability,
                self.provider,
                self.approval_grant,
                self.context_item,
                self.context_consumer,
            )
        ):
            raise ValueError("pre_memory policy context forbids non-memory fields")

        if self.phase is PolicyPhase.PRE_MEMORY_READ:
            if (self.memory_query is None) == (self.memory_record is None):
                raise ValueError("pre_memory_read requires exactly one query or record")
            if self.memory_proposal is not None:
                raise ValueError("pre_memory_read forbids memory_proposal")
            target = self.memory_query or self.memory_record
        elif self.phase is PolicyPhase.PRE_MEMORY_WRITE:
            if self.memory_proposal is None:
                raise ValueError("pre_memory_write requires memory_proposal")
            if self.memory_query is not None or self.memory_record is not None:
                raise ValueError("pre_memory_write forbids memory_query and memory_record")
            target = self.memory_proposal
        else:
            if self.memory_record is None:
                raise ValueError("pre_memory_delete requires memory_record")
            if self.memory_query is not None or self.memory_proposal is not None:
                raise ValueError("pre_memory_delete forbids memory_query and memory_proposal")
            target = self.memory_record

        if (
            target.tenant_id != self.memory_scope.tenant_id
            or target.subject_id != self.memory_scope.subject_id
        ):
            raise ValueError("memory policy target must match memory_scope")
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
