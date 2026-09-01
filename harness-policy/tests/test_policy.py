"""harness-policy 的阶段一行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ContextConsumer,
    ContextFreshness,
    ContextItem,
    ContextProvenance,
    ContextSensitivity,
    ContextSourceKind,
    ContextSourceRef,
    ContextTrustTier,
    ExecutionMode,
    IdentityContext,
    InvocationContext,
    Request,
    RequestInput,
    RequestTarget,
    TenantContext,
)
from harness_policy import (
    AllowAllPolicy,
    CapabilityPermissionPolicy,
    Policy,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyPhase,
    TenantPolicy,
)
from pydantic import ValidationError


def make_context(
    *,
    request_tenant: str | None = None,
    runtime_tenant: str | None = None,
    scopes: set[str] | None = None,
    capability: str = "finance.query/v1",
) -> PolicyContext:
    request = Request(
        tenant_id=request_tenant,
        input=RequestInput(type="text", content="hello"),
        target=RequestTarget(capability=capability),
    )
    invocation = InvocationContext(
        request=request,
        identity=(
            None if scopes is None else IdentityContext(subject="user", scopes=frozenset(scopes))
        ),
        tenant=(None if runtime_tenant is None else TenantContext(tenant_id=runtime_tenant)),
    )
    descriptor = CapabilityDescriptor(
        id=capability,
        name=capability,
        type=CapabilityType.AGENT,
        version="1.0.0",
    )
    return PolicyContext(invocation=invocation, capability=descriptor)


class RecordingPolicy(Policy):
    def __init__(
        self,
        name: str,
        effect: PolicyEffect,
        calls: list[str],
        constraints: dict | None = None,
    ) -> None:
        self._name = name
        self.effect = effect
        self.calls = calls
        self.constraints = constraints or {}

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        self.calls.append(self.name)
        return PolicyDecision(
            effect=self.effect,
            policy=self.name,
            reason="recorded",
            constraints=self.constraints,
        )


class PolicyModelTests(unittest.TestCase):
    def test_context_is_pre_execute_by_default(self) -> None:
        self.assertEqual(make_context().phase.value, "pre_execute")

    def test_decision_serializes_effect_and_constraints(self) -> None:
        decision = PolicyDecision.allow(
            "test",
            constraints={"nested": {"items": [1]}},
        )
        payload = decision.model_dump(mode="json")
        self.assertEqual(payload["effect"], "allow")
        self.assertEqual(payload["constraints"]["nested"]["items"], [1])

    def test_decision_is_frozen(self) -> None:
        decision = PolicyDecision.allow("test")
        with self.assertRaises(ValidationError):
            decision.effect = PolicyEffect.DENY  # type: ignore[misc]

    def test_pre_context_shape_is_strict_and_typed(self) -> None:
        now = datetime.now(UTC)
        context_item = ContextItem(
            item_id="request-item",
            kind="request",
            content={"goal": "compare"},
            source=ContextSourceRef(
                source_kind=ContextSourceKind.REQUEST,
                source_id="request",
            ),
            provenance=ContextProvenance(producer="policy-test"),
            freshness=ContextFreshness(source_version="v1", observed_at=now),
            trust_tier=ContextTrustTier.USER,
            sensitivity=ContextSensitivity.CONFIDENTIAL,
            created_at=now,
        )
        invocation = make_context().invocation

        decision = PolicyEngine((AllowAllPolicy(),)).evaluate_context(
            invocation,
            context_item,
            ContextConsumer.ROUTE,
        )

        self.assertEqual(decision.effect, PolicyEffect.ALLOW)
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_CONTEXT,
                context_item=context_item,
                context_consumer=ContextConsumer.ROUTE,
                requested_mode=ExecutionMode.AUTO,
            )


class EngineTests(unittest.TestCase):
    def test_empty_engine_defaults_to_allow(self) -> None:
        result = PolicyEngine().evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_empty_engine_can_default_to_deny(self) -> None:
        result = PolicyEngine(default_effect=PolicyEffect.DENY).evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_engine_runs_in_order_and_merges_constraints(self) -> None:
        calls: list[str] = []
        engine = PolicyEngine(
            (
                RecordingPolicy(
                    "one",
                    PolicyEffect.ALLOW,
                    calls,
                    {"a": 1},
                ),
                RecordingPolicy(
                    "two",
                    PolicyEffect.ALLOW,
                    calls,
                    {"b": 2},
                ),
            )
        )

        result = engine.evaluate(make_context())

        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(
            result.model_dump(mode="json")["constraints"],
            {"a": 1, "b": 2},
        )

    def test_engine_short_circuits_on_deny(self) -> None:
        calls: list[str] = []
        engine = PolicyEngine(
            (
                RecordingPolicy("one", PolicyEffect.DENY, calls),
                RecordingPolicy("two", PolicyEffect.ALLOW, calls),
            )
        )

        result = engine.evaluate(make_context())

        self.assertEqual(result.effect, PolicyEffect.DENY)
        self.assertEqual(calls, ["one"])

    def test_engine_rejects_non_policy(self) -> None:
        with self.assertRaises(TypeError):
            PolicyEngine((object(),))  # type: ignore[arg-type]


class BuiltInPolicyTests(unittest.TestCase):
    def test_allow_all(self) -> None:
        result = AllowAllPolicy().evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_tenant_allows_when_tenant_is_optional_and_absent(self) -> None:
        result = TenantPolicy().evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_tenant_requires_runtime_context_when_request_names_tenant(self) -> None:
        result = TenantPolicy().evaluate(make_context(request_tenant="a"))
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_tenant_rejects_mismatch(self) -> None:
        result = TenantPolicy().evaluate(make_context(request_tenant="a", runtime_tenant="b"))
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_tenant_require_tenant(self) -> None:
        result = TenantPolicy(require_tenant=True).evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_tenant_allowed_list(self) -> None:
        allowed = TenantPolicy({"a"}).evaluate(make_context(runtime_tenant="a"))
        denied = TenantPolicy({"a"}).evaluate(make_context(runtime_tenant="b"))
        self.assertEqual(allowed.effect, PolicyEffect.ALLOW)
        self.assertEqual(denied.effect, PolicyEffect.DENY)

    def test_capability_permission_allows_matching_scope(self) -> None:
        policy = CapabilityPermissionPolicy({"finance.query/v1": {"finance.read"}})
        result = policy.evaluate(make_context(scopes={"finance.read"}))
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_capability_permission_denies_missing_scope(self) -> None:
        policy = CapabilityPermissionPolicy({"finance.query/v1": {"finance.read"}})
        result = policy.evaluate(make_context(scopes={"other"}))
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_capability_permission_denies_anonymous(self) -> None:
        policy = CapabilityPermissionPolicy({"finance.query/v1": {"finance.read"}})
        result = policy.evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_capability_permission_is_default_deny_for_unconfigured(self) -> None:
        result = CapabilityPermissionPolicy({}).evaluate(make_context(scopes={"*"}))
        self.assertEqual(result.effect, PolicyEffect.DENY)

    def test_capability_permission_can_allow_unconfigured(self) -> None:
        result = CapabilityPermissionPolicy(
            {},
            allow_unconfigured=True,
        ).evaluate(make_context())
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_capability_permission_supports_global_rule(self) -> None:
        result = CapabilityPermissionPolicy({"*": {"invoke"}}).evaluate(
            make_context(scopes={"invoke"})
        )
        self.assertEqual(result.effect, PolicyEffect.ALLOW)

    def test_identity_wildcard_scope_satisfies_rule(self) -> None:
        policy = CapabilityPermissionPolicy({"finance.query/v1": {"finance.read"}})
        result = policy.evaluate(make_context(scopes={"*"}))
        self.assertEqual(result.effect, PolicyEffect.ALLOW)


if __name__ == "__main__":
    unittest.main()
