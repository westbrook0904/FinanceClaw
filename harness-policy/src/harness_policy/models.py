"""FinanceClaw Policy 的稳定上下文与结构化决策。"""

from __future__ import annotations

from enum import StrEnum

from harness_contracts import (
    ApprovalGrant,
    CapabilityDescriptor,
    ContextConsumer,
    ContextItem,
    ContractModel,
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
    """FinanceClaw 自己负责的治理边界。"""

    PRE_CONTEXT = "pre_context"
    PRE_MEMORY_READ = "pre_memory_read"
    PRE_MEMORY_WRITE = "pre_memory_write"
    PRE_MEMORY_DELETE = "pre_memory_delete"
    PRE_EXECUTE = "pre_execute"


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyContext(ContractModel):
    invocation: InvocationContext
    phase: PolicyPhase = PolicyPhase.PRE_EXECUTE
    capability: CapabilityDescriptor | None = None
    provider: ProviderDescriptor | None = None
    approval_grant: ApprovalGrant | None = None
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
            if any(
                value is not None
                for value in (self.capability, self.provider, self.approval_grant)
            ):
                raise ValueError("pre_context policy context forbids execution fields")
            return self

        if self.context_item is not None or self.context_consumer is not None:
            raise ValueError("context_item and context_consumer are only valid for pre_context")
        if self.capability is None:
            raise ValueError("pre_execute policy context requires capability")
        if self.provider is not None and self.provider.capability_id != self.capability.id:
            raise ValueError("provider capability_id must match capability.id")
        return self

    def _validate_memory_payload(self) -> PolicyContext:
        if self.memory_scope is None:
            raise ValueError("pre_memory policy context requires memory_scope")
        if any(
            value is not None
            for value in (
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
