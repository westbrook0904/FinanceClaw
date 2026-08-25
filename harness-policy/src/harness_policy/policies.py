"""阶段一内置 Policy。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import PolicyContext, PolicyDecision
from .policy import Policy


class AllowAllPolicy(Policy):
    """显式允许所有调用，主要用于开发和组合测试。"""

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.allow(self.name, reason="invocation allowed")


class TenantPolicy(Policy):
    """校验 Request tenant 与 Runtime 解析出的可信 tenant 上下文。"""

    def __init__(
        self,
        allowed_tenants: Iterable[str] | None = None,
        *,
        require_tenant: bool = False,
    ) -> None:
        self._allowed_tenants = (
            None if allowed_tenants is None else frozenset(allowed_tenants)
        )
        self._require_tenant = require_tenant

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        requested = context.invocation.request.tenant_id
        runtime_tenant = context.invocation.tenant
        resolved = runtime_tenant.tenant_id if runtime_tenant is not None else None

        if requested is not None and resolved is None:
            return PolicyDecision.deny(
                self.name,
                reason="runtime tenant context is missing",
            )
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


class CapabilityPermissionPolicy(Policy):
    """依据可信 Identity scopes 判断 Capability 调用权限。"""

    def __init__(
        self,
        permissions: Mapping[str, Iterable[str]],
        *,
        allow_unconfigured: bool = False,
    ) -> None:
        self._permissions = {
            capability: frozenset(scopes)
            for capability, scopes in permissions.items()
        }
        self._allow_unconfigured = allow_unconfigured

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        capability_id = context.capability.id
        required = self._permissions.get(
            capability_id,
            self._permissions.get("*"),
        )

        if required is None:
            if self._allow_unconfigured:
                return PolicyDecision.allow(
                    self.name,
                    reason="capability has no configured permission rule",
                )
            return PolicyDecision.deny(
                self.name,
                reason="capability has no configured permission rule",
            )

        if not required:
            return PolicyDecision.allow(
                self.name,
                reason="capability requires no scopes",
            )

        identity = context.invocation.identity
        if identity is None:
            return PolicyDecision.deny(
                self.name,
                reason="authenticated identity is required",
            )

        granted = identity.scopes
        if "*" not in granted and required.isdisjoint(granted):
            return PolicyDecision.deny(
                self.name,
                reason="required capability scope is missing",
                constraints={"required_scopes": sorted(required)},
            )

        return PolicyDecision.allow(
            self.name,
            reason="capability scope allowed",
            constraints={"required_scopes": sorted(required)},
        )
