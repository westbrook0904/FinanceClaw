"""Policy 链共享的阶段一模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from harness_contracts import CapabilityDescriptor, ContractModel, InvocationContext
from harness_contracts.base import FrozenJsonMapping, NonEmptyString


class PolicyPhase(StrEnum):
    """阶段一保留的策略执行阶段。"""

    PRE_EXECUTE = "pre_execute"


class PolicyEffect(StrEnum):
    """阶段一支持的策略决策。"""

    ALLOW = "allow"
    DENY = "deny"


class PolicyContext(ContractModel):
    """Policy 评估所需的只读调用上下文。"""

    invocation: InvocationContext
    capability: CapabilityDescriptor
    phase: PolicyPhase = PolicyPhase.PRE_EXECUTE


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
