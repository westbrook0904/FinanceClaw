"""FinanceClaw Policy 链执行引擎。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_contracts import (
    ContextConsumer,
    ContextItem,
    InvocationContext,
    JsonValue,
    MemoryQuery,
    MemoryRecord,
    MemorySubjectScope,
    MemoryWriteProposal,
)

from .models import PolicyContext, PolicyDecision, PolicyEffect, PolicyPhase
from .policy import Policy


class PolicyEngine:
    """顺序执行当前 phase 的 Policy，按 DENY > REQUIRE_APPROVAL > ALLOW 聚合。"""

    def __init__(
        self,
        policies: Iterable[Policy] = (),
        *,
        default_effect: PolicyEffect = PolicyEffect.ALLOW,
    ) -> None:
        self._policies = tuple(policies)
        if any(not isinstance(policy, Policy) for policy in self._policies):
            raise TypeError("all policies must implement Policy")
        if not isinstance(default_effect, PolicyEffect):
            raise TypeError("default_effect must be PolicyEffect")
        self._default_effect = default_effect

    @property
    def policies(self) -> tuple[Policy, ...]:
        return self._policies

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if not isinstance(context, PolicyContext):
            raise TypeError("context must be PolicyContext")

        applicable = tuple(policy for policy in self._policies if context.phase in policy.phases)
        if not applicable:
            return self._default_decision(context)

        constraints: dict[str, JsonValue] = {}
        approval: PolicyDecision | None = None
        for policy in applicable:
            decision = policy.evaluate(context)
            if not isinstance(decision, PolicyDecision):
                raise TypeError(f"policy {policy.name} must return PolicyDecision")
            constraints.update(decision.model_dump(mode="json")["constraints"])
            if decision.effect is PolicyEffect.DENY:
                return PolicyDecision.deny(
                    decision.policy,
                    reason=decision.reason or "policy denied invocation",
                    constraints=constraints,
                )
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL and approval is None:
                approval = decision

        if approval is not None:
            return PolicyDecision.require_approval(
                approval.policy,
                reason=approval.reason or "policy requires approval",
                constraints=constraints,
            )
        return PolicyDecision.allow(
            "policy-engine",
            reason="all applicable policies allowed the operation",
            constraints=constraints,
        )

    def evaluate_context(
        self,
        invocation: InvocationContext,
        item: ContextItem,
        consumer: ContextConsumer,
    ) -> PolicyDecision:
        return self.evaluate(
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_CONTEXT,
                context_item=item,
                context_consumer=consumer,
            )
        )

    def evaluate_memory_read(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        *,
        query: MemoryQuery | None = None,
        record: MemoryRecord | None = None,
    ) -> PolicyDecision:
        return self.evaluate(
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_MEMORY_READ,
                memory_scope=scope,
                memory_query=query,
                memory_record=record,
            )
        )

    def evaluate_memory_write(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        proposal: MemoryWriteProposal,
    ) -> PolicyDecision:
        return self.evaluate(
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_MEMORY_WRITE,
                memory_scope=scope,
                memory_proposal=proposal,
            )
        )

    def evaluate_memory_delete(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        record: MemoryRecord,
    ) -> PolicyDecision:
        return self.evaluate(
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_MEMORY_DELETE,
                memory_scope=scope,
                memory_record=record,
            )
        )

    def _default_decision(self, context: PolicyContext) -> PolicyDecision:
        reason = f"no policies configured for {context.phase.value}"
        if self._default_effect is PolicyEffect.DENY:
            return PolicyDecision.deny("policy-engine", reason=reason)
        if self._default_effect is PolicyEffect.REQUIRE_APPROVAL:
            return PolicyDecision.require_approval("policy-engine", reason=reason)
        return PolicyDecision.allow("policy-engine", reason=reason)
