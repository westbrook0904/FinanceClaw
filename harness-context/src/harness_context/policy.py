"""基础 Context 安全规则与统一 PolicyEngine PRE_CONTEXT 适配。"""

from __future__ import annotations

from datetime import datetime

from harness_contracts import (
    ContextConsumer,
    ContextError,
    ContextItem,
    ContextSensitivity,
    ContextSourceKind,
    ContextTrustTier,
    ErrorCode,
    InvocationContext,
)
from harness_policy import PolicyEffect, PolicyEngine

_ALLOWED_TRUST = {
    ContextSourceKind.SYSTEM_INSTRUCTION: frozenset({ContextTrustTier.SYSTEM}),
    ContextSourceKind.REQUEST: frozenset({ContextTrustTier.USER}),
    ContextSourceKind.SESSION: frozenset({ContextTrustTier.APPLICATION, ContextTrustTier.USER}),
    ContextSourceKind.MEMORY: frozenset({ContextTrustTier.DATA}),
    ContextSourceKind.CAPABILITY_CATALOG: frozenset({ContextTrustTier.APPLICATION}),
    ContextSourceKind.OBSERVATION: frozenset({ContextTrustTier.DATA}),
}


class ContextPolicy:
    """先执行不可放宽的基础规则，再允许统一 PolicyEngine 进一步收紧。"""

    def __init__(self, policy_engine: PolicyEngine) -> None:
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        self._policy_engine = policy_engine

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    def filter(
        self,
        items: tuple[ContextItem, ...],
        invocation: InvocationContext,
        consumer: ContextConsumer,
        *,
        evaluated_at: datetime,
    ) -> tuple[ContextItem, ...]:
        allowed: list[ContextItem] = []
        for item in items:
            if not self._passes_base_rules(item, evaluated_at=evaluated_at):
                continue
            decision = self._policy_engine.evaluate_context(
                invocation,
                item,
                consumer,
            )
            if decision.effect is PolicyEffect.DENY:
                continue
            if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                raise ContextError(
                    "PRE_CONTEXT policy approval is not supported",
                    code=ErrorCode.CONTEXT_POLICY_UNSUPPORTED,
                    details={"policy": decision.policy},
                )
            allowed.append(item)
        return tuple(allowed)

    @staticmethod
    def _passes_base_rules(item: ContextItem, *, evaluated_at: datetime) -> bool:
        if item.sensitivity is ContextSensitivity.SECRET:
            return False
        if item.expires_at is not None and item.expires_at <= evaluated_at:
            return False
        return item.trust_tier in _ALLOWED_TRUST[item.source.source_kind]
