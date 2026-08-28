"""Stage 3B PRE_ROUTE Policy 与安全 constraint reducer 测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    ApprovalGrant,
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    PlanNode,
    PolicyError,
    ProviderDescriptor,
    Request,
    RequestInput,
    RouteDecision,
    RouteSource,
    RouteType,
    RoutingError,
    TenantContext,
)
from harness_policy import (
    Policy,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyPhase,
    RoutePolicyConstraintReducer,
    RoutePolicyConstraints,
    TenantPolicy,
    reduce_route_policy_constraints,
)
from harness_routing import Router, RoutingContext
from pydantic import ValidationError


def invocation(mode: ExecutionMode = ExecutionMode.AUTO) -> InvocationContext:
    return InvocationContext(
        request=Request(
            request_id="pre-route-request",
            input=RequestInput(type="goal", content="compare providers"),
            options={"execution_mode": mode},
        )
    )


class RecordingPreRoutePolicy(Policy):
    def __init__(
        self,
        name: str,
        *,
        effect: PolicyEffect = PolicyEffect.ALLOW,
        constraints: dict | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self._name = name
        self._effect = effect
        self._constraints = constraints or {}
        self._calls = calls

    @property
    def name(self) -> str:
        return self._name

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_ROUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if self._calls is not None:
            self._calls.append(self.name)
        if self._effect is PolicyEffect.DENY:
            return PolicyDecision.deny(
                self.name,
                reason="route denied",
                constraints=self._constraints,
            )
        if self._effect is PolicyEffect.REQUIRE_APPROVAL:
            return PolicyDecision.require_approval(
                self.name,
                reason="route approval required",
                constraints=self._constraints,
            )
        return PolicyDecision.allow(
            self.name,
            reason="route allowed",
            constraints=self._constraints,
        )


class PreExecuteOnlyPolicy(Policy):
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        self.calls += 1
        return PolicyDecision.deny(self.name, reason="pre-execute only")


class CountingModelFallbackRouter(Router):
    def __init__(self) -> None:
        self.calls = 0
        self.model_calls = 0

    @property
    def router_id(self) -> str:
        return "counting-model-fallback"

    async def route(self, context: RoutingContext) -> RouteDecision:
        self.calls += 1
        self.model_calls += 1
        return RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.MODEL,
            reason_code="MODEL_ROUTE",
        )


class PreRoutePolicyContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.invocation = invocation()
        self.capability = CapabilityDescriptor(
            id="finance.query/v1",
            name="Finance Query",
            type=CapabilityType.AGENT,
            version="1.0.0",
        )
        self.plan = ExecutionPlan(
            plan_id="policy-plan",
            nodes=(PlanNode(node_id="query", capability="finance.query/v1"),),
        )
        self.provider = ProviderDescriptor(
            provider_id="finance-primary",
            capability_id=self.capability.id,
            plugin_id="finance-plugin",
            implementation_version="1.0.0",
        )
        self.approval_grant = ApprovalGrant(
            approval_id="approval-1",
            plan_id=self.plan.plan_id,
            node_id="query",
            decided_by="operator",
            granted_at=datetime.now(UTC),
        )

    def test_pre_route_requires_requested_mode(self) -> None:
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=self.invocation,
                phase=PolicyPhase.PRE_ROUTE,
            )

    def test_pre_route_forbids_plan_capability_provider_and_approval(self) -> None:
        invalid_payloads = (
            {"plan": self.plan},
            {"capability": self.capability},
            {"provider": self.provider},
            {"approval_grant": self.approval_grant},
        )
        for payload in invalid_payloads:
            with self.subTest(field=next(iter(payload))):
                with self.assertRaises(ValidationError):
                    PolicyContext(
                        invocation=self.invocation,
                        phase=PolicyPhase.PRE_ROUTE,
                        requested_mode=ExecutionMode.AUTO,
                        **payload,
                    )

    def test_requested_mode_is_rejected_outside_pre_route(self) -> None:
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=self.invocation,
                phase=PolicyPhase.PRE_PLAN,
                plan=self.plan,
                requested_mode=ExecutionMode.PLAN,
            )
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=self.invocation,
                phase=PolicyPhase.PRE_EXECUTE,
                capability=self.capability,
                requested_mode=ExecutionMode.FAST,
            )


class RoutePolicyConstraintReducerTests(unittest.TestCase):
    def test_reducer_intersects_sets_and_takes_lower_limits(self) -> None:
        constraints = reduce_route_policy_constraints(
            (
                {
                    "forced_mode": "plan",
                    "allowed_modes": ["fast", "plan"],
                    "allowed_capability_ids": ["a/v1", "b/v1"],
                    "allowed_planner_ids": ["planner-a", "planner-b"],
                    "max_plan_attempts": 3,
                    "max_plan_nodes": 20,
                },
                {
                    "forced_mode": "plan",
                    "allowed_modes": ["plan", "explore"],
                    "allowed_capability_ids": ["b/v1", "c/v1"],
                    "allowed_planner_ids": ["planner-b"],
                    "max_plan_attempts": 2,
                    "max_plan_nodes": 10,
                },
            )
        )

        self.assertEqual(constraints.forced_mode, ExecutionMode.PLAN)
        self.assertIsInstance(constraints, RoutePolicyConstraints)
        self.assertEqual(constraints.allowed_modes, frozenset({ExecutionMode.PLAN}))
        self.assertEqual(constraints.allowed_capability_ids, frozenset({"b/v1"}))
        self.assertEqual(constraints.allowed_planner_ids, frozenset({"planner-b"}))
        self.assertEqual(constraints.max_plan_attempts, 2)
        self.assertEqual(constraints.max_plan_nodes, 10)

    def test_reducer_preserves_explicit_empty_capability_and_planner_sets(self) -> None:
        constraints = reduce_route_policy_constraints(
            (
                {"allowed_capability_ids": ["a/v1"]},
                {
                    "allowed_capability_ids": ["b/v1"],
                    "allowed_planner_ids": [],
                },
            )
        )

        self.assertEqual(constraints.allowed_capability_ids, frozenset())
        self.assertEqual(constraints.allowed_planner_ids, frozenset())

    def test_conflicting_forced_modes_fail_closed(self) -> None:
        reducer = RoutePolicyConstraintReducer()
        reducer.add({"forced_mode": "fast"})

        with self.assertRaises(RoutingError) as raised:
            reducer.add({"forced_mode": "plan"})

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_ALLOWED)
        self.assertEqual(reducer.constraints.forced_mode, ExecutionMode.FAST)

    def test_empty_allowed_mode_intersection_fails_closed(self) -> None:
        with self.assertRaises(RoutingError) as raised:
            reduce_route_policy_constraints(
                (
                    {"allowed_modes": ["fast"]},
                    {"allowed_modes": ["plan"]},
                )
            )

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_ALLOWED)

    def test_forced_mode_excluded_by_allowed_modes_fails_closed(self) -> None:
        with self.assertRaises(RoutingError) as raised:
            reduce_route_policy_constraints(({"forced_mode": "plan", "allowed_modes": ["fast"]},))

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_ALLOWED)

    def test_unknown_or_invalid_constraint_payload_fails_closed_without_echo(self) -> None:
        invalid_payloads = (
            {"future_constraint": "secret-value"},
            {"max_plan_nodes": 0},
            {"allowed_modes": ["not-a-mode"]},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=tuple(payload)):
                with self.assertRaises(PolicyError) as raised:
                    reduce_route_policy_constraints((payload,))
                self.assertEqual(raised.exception.code, ErrorCode.POLICY_DENIED)
                self.assertEqual(
                    raised.exception.details["reason"],
                    "invalid_route_constraints",
                )
                self.assertNotIn("secret-value", str(raised.exception.details))

        with self.assertRaises(PolicyError) as non_mapping:
            reduce_route_policy_constraints((["invalid"],))  # type: ignore[arg-type]
        self.assertEqual(non_mapping.exception.code, ErrorCode.POLICY_DENIED)


class PreRoutePolicyEngineTests(unittest.TestCase):
    def test_engine_uses_safe_reducer_only_for_pre_route(self) -> None:
        engine = PolicyEngine(
            (
                RecordingPreRoutePolicy(
                    "first",
                    constraints={
                        "allowed_modes": ["fast", "plan"],
                        "max_plan_nodes": 20,
                    },
                ),
                RecordingPreRoutePolicy(
                    "second",
                    constraints={
                        "allowed_modes": ["plan"],
                        "max_plan_nodes": 10,
                    },
                ),
            )
        )

        result = engine.evaluate_pre_route(invocation(), ExecutionMode.AUTO)

        self.assertEqual(result.effective_mode, ExecutionMode.AUTO)
        self.assertEqual(result.constraints.allowed_modes, frozenset({ExecutionMode.PLAN}))
        self.assertEqual(result.constraints.max_plan_nodes, 10)

    def test_policy_can_force_auto_to_fast_or_plan(self) -> None:
        for forced_mode in (ExecutionMode.FAST, ExecutionMode.PLAN):
            with self.subTest(forced_mode=forced_mode):
                engine = PolicyEngine(
                    (
                        RecordingPreRoutePolicy(
                            "force-mode",
                            constraints={"forced_mode": forced_mode.value},
                        ),
                    )
                )

                result = engine.evaluate_pre_route(invocation(), ExecutionMode.AUTO)

                self.assertEqual(result.effective_mode, forced_mode)
                self.assertEqual(result.constraints.forced_mode, forced_mode)

    def test_policy_cannot_change_explicit_mode(self) -> None:
        engine = PolicyEngine(
            (
                RecordingPreRoutePolicy(
                    "force-plan",
                    constraints={"forced_mode": "plan"},
                ),
            )
        )

        with self.assertRaises(RoutingError) as raised:
            engine.evaluate_pre_route(invocation(ExecutionMode.FAST), ExecutionMode.FAST)

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_ALLOWED)

    def test_policy_can_forbid_plan(self) -> None:
        engine = PolicyEngine(
            (
                RecordingPreRoutePolicy(
                    "fast-only",
                    constraints={"allowed_modes": ["fast"]},
                ),
            )
        )

        with self.assertRaises(RoutingError) as raised:
            engine.evaluate_pre_route(invocation(ExecutionMode.PLAN), ExecutionMode.PLAN)

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_ALLOWED)

    def test_pre_route_approval_is_not_a_waiting_state(self) -> None:
        engine = PolicyEngine(
            (
                RecordingPreRoutePolicy(
                    "approval",
                    effect=PolicyEffect.REQUIRE_APPROVAL,
                ),
            )
        )

        with self.assertRaises(RoutingError) as raised:
            engine.evaluate_pre_route(invocation(), ExecutionMode.AUTO)

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_APPROVAL_NOT_SUPPORTED)

    def test_pre_execute_only_policy_is_not_called_during_pre_route(self) -> None:
        policy = PreExecuteOnlyPolicy()

        result = PolicyEngine((policy,)).evaluate_pre_route(
            invocation(),
            ExecutionMode.AUTO,
        )

        self.assertEqual(result.decision.effect, PolicyEffect.ALLOW)
        self.assertEqual(policy.calls, 0)

    def test_tenant_policy_governs_pre_route_without_emitting_untyped_constraint(self) -> None:
        request = Request(
            tenant_id="tenant-a",
            input=RequestInput(type="goal", content="compare"),
        )
        trusted_invocation = InvocationContext(
            request=request,
            tenant=TenantContext(tenant_id="tenant-a"),
        )

        result = PolicyEngine((TenantPolicy({"tenant-a"}),)).evaluate_pre_route(
            trusted_invocation,
            ExecutionMode.AUTO,
        )

        self.assertEqual(result.decision.effect, PolicyEffect.ALLOW)
        self.assertEqual(result.constraints, RoutePolicyConstraints())


class PreRouteDenyBeforeRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_short_circuits_before_router_and_model_fallback(self) -> None:
        calls: list[str] = []
        engine = PolicyEngine(
            (
                RecordingPreRoutePolicy(
                    "deny",
                    effect=PolicyEffect.DENY,
                    calls=calls,
                ),
                RecordingPreRoutePolicy("must-not-run", calls=calls),
            )
        )
        router = CountingModelFallbackRouter()

        async def governed_route() -> RouteDecision:
            engine.evaluate_pre_route(invocation(), ExecutionMode.AUTO)
            return await router.route(None)  # type: ignore[arg-type]

        with self.assertRaises(PolicyError) as raised:
            await governed_route()

        self.assertEqual(raised.exception.code, ErrorCode.POLICY_DENIED)
        self.assertEqual(calls, ["deny"])
        self.assertEqual(router.calls, 0)
        self.assertEqual(router.model_calls, 0)


if __name__ == "__main__":
    unittest.main()
