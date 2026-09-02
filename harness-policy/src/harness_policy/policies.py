"""阶段二内置通用 Policy。"""

from __future__ import annotations

from collections.abc import Iterable

from .models import PolicyContext, PolicyDecision, PolicyPhase
from .policy import Policy


class AllowAllPolicy(Policy):
    """显式允许全部治理阶段，主要用于开发和组合测试。"""

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset(
            {
                PolicyPhase.PRE_MEMORY_READ,
                PolicyPhase.PRE_MEMORY_WRITE,
                PolicyPhase.PRE_MEMORY_DELETE,
            }
        )

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.allow(self.name, reason=f"{context.phase.value} allowed")


class TenantPolicy(Policy):
    """校验 Request tenant 与 Runtime 解析出的可信 tenant 上下文。"""

    def __init__(
        self,
        allowed_tenants: Iterable[str] | None = None,
        *,
        require_tenant: bool = False,
    ) -> None:
        self._allowed_tenants = None if allowed_tenants is None else frozenset(allowed_tenants)
        self._require_tenant = require_tenant

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset(
            {
                PolicyPhase.PRE_MEMORY_READ,
                PolicyPhase.PRE_MEMORY_WRITE,
                PolicyPhase.PRE_MEMORY_DELETE,
            }
        )

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        requested = context.invocation.request.tenant_id
        runtime_tenant = context.invocation.tenant
        resolved = runtime_tenant.tenant_id if runtime_tenant is not None else None

        if requested is not None and resolved is None:
            return PolicyDecision.deny(self.name, reason="runtime tenant context is missing")
        if requested is not None and resolved is not None and requested != resolved:
            return PolicyDecision.deny(
                self.name,
                reason="request tenant does not match runtime tenant",
            )
        if self._require_tenant and resolved is None:
            return PolicyDecision.deny(self.name, reason="tenant is required")
        if (
            resolved is not None
            and self._allowed_tenants is not None
            and resolved not in self._allowed_tenants
        ):
            return PolicyDecision.deny(self.name, reason="tenant is not allowed")

        constraints = {"tenant_id": resolved} if resolved is not None else {}
        return PolicyDecision.allow(
            self.name,
            reason="tenant allowed",
            constraints=constraints,
        )
