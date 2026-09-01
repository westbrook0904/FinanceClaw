"""统一 PolicyEngine 的 PRE_MEMORY effect 解释。"""

from __future__ import annotations

from harness_contracts import (
    ErrorCode,
    InvocationContext,
    MemoryAccessError,
    MemoryQuery,
    MemoryRecord,
    MemorySubjectScope,
    MemoryWriteProposal,
)
from harness_policy import PolicyDecision, PolicyEffect, PolicyEngine


class MemoryPolicy:
    def __init__(self, policy_engine: PolicyEngine) -> None:
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        self._policy_engine = policy_engine

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    def allows_read(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        *,
        query: MemoryQuery | None = None,
        record: MemoryRecord | None = None,
    ) -> bool:
        decision = self._policy_engine.evaluate_memory_read(
            invocation,
            scope,
            query=query,
            record=record,
        )
        return self._interpret(decision, operation="read", deny_as_false=True)

    def require_write(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        proposal: MemoryWriteProposal,
    ) -> None:
        decision = self._policy_engine.evaluate_memory_write(invocation, scope, proposal)
        self._interpret(decision, operation="write", deny_as_false=False)

    def require_delete(
        self,
        invocation: InvocationContext,
        scope: MemorySubjectScope,
        record: MemoryRecord,
    ) -> None:
        decision = self._policy_engine.evaluate_memory_delete(invocation, scope, record)
        self._interpret(decision, operation="delete", deny_as_false=False)

    @staticmethod
    def _interpret(
        decision: PolicyDecision,
        *,
        operation: str,
        deny_as_false: bool,
    ) -> bool:
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            raise MemoryAccessError(
                "memory policy approval is not supported",
                code=ErrorCode.MEMORY_POLICY_UNSUPPORTED,
                details={"operation": operation, "policy": decision.policy},
            )
        if decision.effect is PolicyEffect.DENY:
            if deny_as_false:
                return False
            raise MemoryAccessError(
                "memory operation denied by policy",
                code=ErrorCode.MEMORY_POLICY_DENIED,
                details={"operation": operation, "policy": decision.policy},
            )
        return True
