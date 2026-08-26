"""阶段二 Policy 链执行引擎。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_contracts import JsonValue

from .models import PolicyContext, PolicyDecision, PolicyEffect
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

        applicable = tuple(
            policy for policy in self._policies if context.phase in policy.phases
        )
        if not applicable:
            return self._default_decision(context)

        constraints: dict[str, JsonValue] = {}
        approval: PolicyDecision | None = None
        for policy in applicable:
            decision = policy.evaluate(context)
            if not isinstance(decision, PolicyDecision):
                raise TypeError(f"policy {policy.name} must return PolicyDecision")

            serialized = decision.model_dump(mode="json")["constraints"]
            constraints.update(serialized)
            if decision.effect is PolicyEffect.DENY:
                return PolicyDecision.deny(
                    decision.policy,
                    reason=decision.reason or "policy denied invocation",
                    constraints=constraints,
                )
            if (
                decision.effect is PolicyEffect.REQUIRE_APPROVAL
                and approval is None
            ):
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

    def _default_decision(self, context: PolicyContext) -> PolicyDecision:
        reason = f"no policies configured for {context.phase.value}"
        if self._default_effect is PolicyEffect.DENY:
            return PolicyDecision.deny("policy-engine", reason=reason)
        if self._default_effect is PolicyEffect.REQUIRE_APPROVAL:
            return PolicyDecision.require_approval("policy-engine", reason=reason)
        return PolicyDecision.allow("policy-engine", reason=reason)
