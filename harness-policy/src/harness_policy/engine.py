"""阶段一 Policy 链执行引擎。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_contracts import JsonValue

from .models import PolicyContext, PolicyDecision, PolicyEffect
from .policy import Policy


class PolicyEngine:
    """顺序执行 Policy，并在首个 DENY 决策处短路。"""

    def __init__(
        self,
        policies: Iterable[Policy] = (),
        *,
        default_effect: PolicyEffect = PolicyEffect.ALLOW,
    ) -> None:
        self._policies = tuple(policies)
        if any(not isinstance(policy, Policy) for policy in self._policies):
            raise TypeError("all policies must implement Policy")
        self._default_effect = default_effect

    @property
    def policies(self) -> tuple[Policy, ...]:
        """返回不可变的 Policy 链。"""

        return self._policies

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """执行策略链，并聚合所有 ALLOW 决策携带的约束。"""

        if not self._policies:
            if self._default_effect is PolicyEffect.DENY:
                return PolicyDecision.deny(
                    "policy-engine",
                    reason="no policy allowed the invocation",
                )
            return PolicyDecision.allow(
                "policy-engine",
                reason="no policies configured",
            )

        constraints: dict[str, JsonValue] = {}
        for policy in self._policies:
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

        return PolicyDecision.allow(
            "policy-engine",
            reason="all policies allowed the invocation",
            constraints=constraints,
        )
