"""阶段二内置通用 Policy。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from harness_contracts import EgressType, SideEffectType

from .models import PolicyContext, PolicyDecision, PolicyPhase
from .policy import Policy


class AllowAllPolicy(Policy):
    """显式允许全部治理阶段，主要用于开发和组合测试。"""

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset(
            {
                PolicyPhase.PRE_CONTEXT,
                PolicyPhase.PRE_MEMORY_READ,
                PolicyPhase.PRE_MEMORY_WRITE,
                PolicyPhase.PRE_MEMORY_DELETE,
                PolicyPhase.PRE_EXECUTE,
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
                PolicyPhase.PRE_CONTEXT,
                PolicyPhase.PRE_MEMORY_READ,
                PolicyPhase.PRE_MEMORY_WRITE,
                PolicyPhase.PRE_MEMORY_DELETE,
                PolicyPhase.PRE_EXECUTE,
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


class CapabilityPermissionPolicy(Policy):
    """依据可信 Identity scopes 判断 Capability 调用权限。"""

    def __init__(
        self,
        permissions: Mapping[str, Iterable[str]],
        *,
        allow_unconfigured: bool = False,
    ) -> None:
        self._permissions = {
            capability: frozenset(scopes) for capability, scopes in permissions.items()
        }
        self._allow_unconfigured = allow_unconfigured

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        capability = context.capability
        if capability is None:
            return PolicyDecision.allow(self.name, reason="not a capability boundary")
        capability_id = capability.id
        required = self._permissions.get(capability_id, self._permissions.get("*"))

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
            return PolicyDecision.allow(self.name, reason="capability requires no scopes")

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


class RequireApprovalPolicy(Policy):
    """按 Capability/副作用/egress 要求一次 Human Approval。"""

    def __init__(
        self,
        *,
        capabilities: Iterable[str] = (),
        side_effects: Iterable[SideEffectType] = (),
        egress: Iterable[EgressType] = (),
        reason: str = "capability requires human approval",
    ) -> None:
        self._capabilities = frozenset(capabilities)
        self._side_effects = frozenset(side_effects)
        self._egress = frozenset(egress)
        if not isinstance(reason, str) or not reason.strip():
            raise TypeError("reason must be a non-empty string")
        self._reason = reason.strip()

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        capability = context.capability
        if capability is None or not self._matches(context):
            return PolicyDecision.allow(self.name, reason="approval rule did not match")
        if context.approval_grant is not None:
            return PolicyDecision.allow(
                self.name,
                reason="matching approval grant supplied",
                constraints={"approval_id": context.approval_grant.approval_id},
            )
        profile = capability.execution_profile
        return PolicyDecision.require_approval(
            self.name,
            reason=self._reason,
            constraints={
                "capability": capability.id,
                "side_effect": profile.side_effect.value,
                "egress": profile.egress.value,
            },
        )

    def _matches(self, context: PolicyContext) -> bool:
        capability = context.capability
        if capability is None:
            return False
        profile = capability.execution_profile
        selectors_configured = bool(self._capabilities or self._side_effects or self._egress)
        if not selectors_configured:
            return True
        return (
            capability.id in self._capabilities
            or "*" in self._capabilities
            or profile.side_effect in self._side_effects
            or profile.egress in self._egress
        )
