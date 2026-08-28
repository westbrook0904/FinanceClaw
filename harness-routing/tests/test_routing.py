"""Routing Foundation、RuleRouter 与独立 Validator 的行为测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionMode,
    IdentityContext,
    InvocationContext,
    PlanningError,
    Request,
    RequestError,
    RequestInput,
    RequestOptions,
    RequestTarget,
    RouteDecision,
    RouteSource,
    RouteType,
    RoutingError,
)
from harness_routing import (
    InputTypeRouteRule,
    RequestSummary,
    RouteDecisionValidator,
    RoutePolicyConstraints,
    Router,
    RoutingContext,
    RuleRouter,
    SafeRequestProjector,
)
from pydantic import ValidationError


def descriptor(capability_id: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id,
        name=capability_id,
        type=CapabilityType.TOOL,
        version="1.0.0",
    )


def make_context(
    *,
    mode: ExecutionMode = ExecutionMode.AUTO,
    input_type: str = "text",
    target: str | None = None,
    catalog_ids: tuple[str, ...] = ("echo.reply/v1", "finance.query/v1"),
    constraints: RoutePolicyConstraints | None = None,
) -> RoutingContext:
    request = Request(
        request_id="req-route",
        input=RequestInput(type=input_type, content={"query": "hello"}),
        target=RequestTarget(capability=target) if target is not None else None,
        options=RequestOptions(execution_mode=mode),
    )
    return RoutingContext(
        invocation=InvocationContext(request=request),
        request_summary=SafeRequestProjector().project(request),
        requested_mode=mode,
        catalog_snapshot=tuple(descriptor(item) for item in catalog_ids),
        constraints=constraints or RoutePolicyConstraints(),
    )


class RequestProjectionTests(unittest.TestCase):
    def test_projector_allowlists_metadata_and_omits_sensitive_context(self) -> None:
        request = Request(
            request_id="req-summary",
            tenant_id="untrusted-tenant",
            user_id="untrusted-user",
            input=RequestInput(type="goal", content={"query": "compare"}),
            target=RequestTarget(capability="finance.query/v1", plugin="private-plugin"),
            metadata={
                "locale": "zh-CN",
                "secret": "do-not-project",
                "nested": {"items": [1, 2]},
            },
        )
        invocation = InvocationContext(
            request=request,
            identity=IdentityContext(subject="trusted-user", scopes={"finance.read"}),
            attributes={"tenant_secret": "not-visible"},
        )
        projector = SafeRequestProjector(metadata_allowlist={"locale", "nested"})

        summary = projector.project(request)
        payload = summary.model_dump(mode="json")

        self.assertEqual(payload["metadata"], {"locale": "zh-CN", "nested": {"items": [1, 2]}})
        self.assertEqual(summary.target_capability, "finance.query/v1")
        self.assertNotIn("plugin", payload)
        self.assertNotIn("secret", payload["metadata"])
        self.assertNotIn("identity", payload)
        self.assertNotIn("attributes", payload)
        self.assertNotIn(invocation.identity.subject, str(payload))
        with self.assertRaises(TypeError):
            summary.metadata["new"] = True  # type: ignore[index]

    def test_projector_enforces_each_summary_limit_without_leaking_content(self) -> None:
        cases = (
            (
                SafeRequestProjector(max_depth=1),
                {"nested": {"value": "too-deep"}},
                "max_depth",
            ),
            (
                SafeRequestProjector(max_collection_items=1),
                [1, 2],
                "max_collection_items",
            ),
            (
                SafeRequestProjector(max_string_length=3),
                "secret-value",
                "max_string_length",
            ),
            (
                SafeRequestProjector(max_total_values=2),
                [1, 2],
                "max_total_values",
            ),
        )

        for projector, content, expected_limit in cases:
            with self.subTest(limit=expected_limit):
                request = Request(input=RequestInput(type="json", content=content))
                with self.assertRaises(RequestError) as raised:
                    projector.project(request)
                self.assertEqual(raised.exception.code, ErrorCode.REQUEST_INVALID)
                self.assertEqual(raised.exception.details["limit"], expected_limit)
                self.assertNotIn("secret-value", str(raised.exception.details))

    def test_disallowed_metadata_is_not_subject_to_egress_projection(self) -> None:
        request = Request(
            input=RequestInput(type="text", content="ok"),
            metadata={"secret": "x" * 10_000},
        )

        summary = SafeRequestProjector(max_string_length=10).project(request)

        self.assertEqual(summary.metadata, {})

    def test_projector_rejects_a_string_as_metadata_allowlist(self) -> None:
        with self.assertRaises(TypeError):
            SafeRequestProjector(metadata_allowlist="locale")


class RoutingContextTests(unittest.TestCase):
    def test_context_rejects_inconsistent_summary_and_duplicate_catalog(self) -> None:
        context = make_context(target="echo.reply/v1")
        request = context.invocation.request

        with self.assertRaises(ValidationError):
            RoutingContext(
                invocation=context.invocation,
                request_summary=RequestSummary(
                    request_id=request.request_id,
                    input_type=request.input.type,
                    input_content="redacted",
                    target_capability="finance.query/v1",
                ),
                requested_mode=ExecutionMode.AUTO,
                catalog_snapshot=context.catalog_snapshot,
            )

        with self.assertRaises(ValidationError):
            RoutingContext(
                invocation=context.invocation,
                request_summary=context.request_summary,
                requested_mode=ExecutionMode.AUTO,
                catalog_snapshot=(descriptor("echo.reply/v1"), descriptor("echo.reply/v1")),
            )

    def test_policy_constraint_budgets_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError):
            RoutePolicyConstraints(max_plan_attempts=0)
        with self.assertRaises(ValidationError):
            RoutePolicyConstraints(max_plan_nodes=0)


class StubFallbackRouter(Router):
    def __init__(
        self,
        decision: RouteDecision | None = None,
        error: RoutingError | None = None,
    ) -> None:
        self.calls = 0
        self.contexts: list[RoutingContext] = []
        self._decision = decision
        self._error = error

    @property
    def router_id(self) -> str:
        return "stub-fallback"

    async def route(self, context: RoutingContext) -> RouteDecision:
        self.calls += 1
        self.contexts.append(context)
        if self._error is not None:
            raise self._error
        if self._decision is None:
            raise AssertionError("test fallback requires a decision or error")
        return self._decision


class RuleRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_target_wins_before_configured_input_rule(self) -> None:
        router = RuleRouter(
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="text",
                    mode=ExecutionMode.PLAN,
                    planner_id="configured-planner",
                ),
            )
        )

        decision = await router.route(make_context(target="echo.reply/v1"))

        self.assertEqual(decision.mode, ExecutionMode.FAST)
        self.assertEqual(decision.capability_id, "echo.reply/v1")
        self.assertEqual(decision.source, RouteSource.REQUEST)
        self.assertEqual(decision.reason_code, "EXPLICIT_TARGET")

    async def test_plan_mode_uses_default_planner_before_input_rule(self) -> None:
        router = RuleRouter(
            default_planner_id="default-planner",
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="text",
                    mode=ExecutionMode.FAST,
                    capability_id="echo.reply/v1",
                ),
            ),
        )

        decision = await router.route(make_context(mode=ExecutionMode.PLAN, target="echo.reply/v1"))

        self.assertEqual(decision.mode, ExecutionMode.PLAN)
        self.assertEqual(decision.planner_id, "default-planner")
        self.assertEqual(decision.source, RouteSource.REQUEST)

    async def test_fast_without_target_uses_only_fast_input_rule(self) -> None:
        router = RuleRouter(
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="command",
                    mode=ExecutionMode.FAST,
                    capability_id="echo.reply/v1",
                    reason_code="COMMAND_RULE",
                ),
            )
        )

        decision = await router.route(make_context(mode=ExecutionMode.FAST, input_type="command"))

        self.assertEqual(decision.mode, ExecutionMode.FAST)
        self.assertEqual(decision.capability_id, "echo.reply/v1")
        self.assertEqual(decision.source, RouteSource.RULE)
        self.assertEqual(decision.reason_code, "COMMAND_RULE")

    async def test_auto_rule_can_choose_plan(self) -> None:
        router = RuleRouter(
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="goal",
                    mode=ExecutionMode.PLAN,
                    planner_id="static-planner",
                ),
            )
        )

        decision = await router.route(make_context(input_type="goal"))

        self.assertEqual(decision.mode, ExecutionMode.PLAN)
        self.assertEqual(decision.planner_id, "static-planner")

    async def test_no_match_is_explicit_and_does_not_guess_capability(self) -> None:
        with self.assertRaises(RoutingError) as raised:
            await RuleRouter().route(make_context())

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_NO_MATCH)
        self.assertNotIn("capability_id", raised.exception.details)

    async def test_plan_without_default_planner_is_explicit(self) -> None:
        with self.assertRaises(PlanningError) as raised:
            await RuleRouter().route(make_context(mode=ExecutionMode.PLAN))

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_NOT_CONFIGURED)

    async def test_fallback_is_last_and_its_failure_is_not_reinterpreted(self) -> None:
        failure = RoutingError("model failed", code=ErrorCode.ROUTE_MODEL_FAILED)
        fallback = StubFallbackRouter(error=failure)
        router = RuleRouter(fallback=fallback)

        with self.assertRaises(RoutingError) as raised:
            await router.route(make_context())

        self.assertIs(raised.exception, failure)
        self.assertEqual(fallback.calls, 1)

    async def test_fallback_decision_is_returned_without_rewriting(self) -> None:
        fallback_decision = RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.MODEL,
            planner_id="llm-planner",
            reason_code="MODEL_ROUTE",
        )
        fallback = StubFallbackRouter(decision=fallback_decision)

        decision = await RuleRouter(fallback=fallback).route(make_context())

        self.assertIs(decision, fallback_decision)
        self.assertEqual(fallback.calls, 1)

    async def test_explore_and_hybrid_produce_contract_decisions_for_guard(self) -> None:
        router = RuleRouter(default_planner_id="default-planner")
        validator = RouteDecisionValidator(planner_ids={"default-planner"})

        for mode in (ExecutionMode.EXPLORE, ExecutionMode.HYBRID):
            with self.subTest(mode=mode):
                context = make_context(mode=mode)
                decision = await router.route(context)
                self.assertEqual(decision.mode, mode)
                with self.assertRaises(RoutingError) as raised:
                    validator.validate(decision, context)
                self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODE_NOT_AVAILABLE)


class RouteDecisionValidatorTests(unittest.TestCase):
    def test_validator_rejects_a_string_as_planner_collection(self) -> None:
        with self.assertRaises(TypeError):
            RouteDecisionValidator(planner_ids="default-planner")

    def test_valid_fast_and_plan_decisions_pass(self) -> None:
        validator = RouteDecisionValidator(planner_ids={"default-planner"})
        fast_context = make_context(target="echo.reply/v1")
        fast = RouteDecision(
            mode=ExecutionMode.FAST,
            route_type=RouteType.DIRECT_CAPABILITY,
            source=RouteSource.REQUEST,
            capability_id="echo.reply/v1",
            reason_code="EXPLICIT_TARGET",
        )
        plan_context = make_context(mode=ExecutionMode.PLAN)
        plan = RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.REQUEST,
            planner_id="default-planner",
            reason_code="REQUEST_MODE_PLAN",
        )

        self.assertIs(validator.validate(fast, fast_context), fast)
        self.assertIs(validator.validate(plan, plan_context), plan)

    def test_validator_rechecks_schema_and_fixed_request_mode(self) -> None:
        validator = RouteDecisionValidator(planner_ids={"default-planner"})
        invalid = RouteDecision.model_construct(
            mode=ExecutionMode.AUTO,
            route_type=RouteType.DIRECT_CAPABILITY,
            source=RouteSource.RULE,
            capability_id="echo.reply/v1",
            reason_code="BYPASS",
        )

        with self.assertRaises(RoutingError) as schema_error:
            validator.validate(invalid, make_context())
        self.assertEqual(schema_error.exception.code, ErrorCode.ROUTE_INVALID_DECISION)

        plan = RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.MODEL,
            planner_id="default-planner",
            reason_code="MODE_CHANGED",
        )
        with self.assertRaises(RoutingError) as mode_error:
            validator.validate(plan, make_context(mode=ExecutionMode.FAST))
        self.assertEqual(mode_error.exception.code, ErrorCode.ROUTE_INVALID_DECISION)

    def test_catalog_and_explicit_target_are_authoritative(self) -> None:
        validator = RouteDecisionValidator()
        missing = RouteDecision(
            mode=ExecutionMode.FAST,
            route_type=RouteType.DIRECT_CAPABILITY,
            source=RouteSource.MODEL,
            capability_id="missing/v1",
            reason_code="MODEL_SELECTED",
        )
        with self.assertRaises(RoutingError) as missing_error:
            validator.validate(missing, make_context())
        self.assertEqual(missing_error.exception.code, ErrorCode.ROUTE_INVALID_DECISION)

        rewritten = RouteDecision(
            mode=ExecutionMode.FAST,
            route_type=RouteType.DIRECT_CAPABILITY,
            source=RouteSource.MODEL,
            capability_id="finance.query/v1",
            reason_code="MODEL_REWROTE_TARGET",
        )
        with self.assertRaises(RoutingError) as target_error:
            validator.validate(rewritten, make_context(target="echo.reply/v1"))
        self.assertEqual(target_error.exception.code, ErrorCode.ROUTE_INVALID_DECISION)

    def test_planner_must_exist_in_local_snapshot(self) -> None:
        decision = RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.MODEL,
            planner_id="missing-planner",
            reason_code="MODEL_SELECTED",
        )

        with self.assertRaises(PlanningError) as raised:
            RouteDecisionValidator(planner_ids={"other-planner"}).validate(
                decision,
                make_context(mode=ExecutionMode.PLAN),
            )

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_NOT_CONFIGURED)

    def test_policy_constraints_fail_closed_with_specific_codes(self) -> None:
        cases = (
            (
                RoutePolicyConstraints(forced_mode=ExecutionMode.PLAN),
                RouteDecision(
                    mode=ExecutionMode.FAST,
                    route_type=RouteType.DIRECT_CAPABILITY,
                    source=RouteSource.RULE,
                    capability_id="echo.reply/v1",
                    reason_code="RULE",
                ),
                ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                set(),
            ),
            (
                RoutePolicyConstraints(allowed_modes=frozenset({ExecutionMode.FAST})),
                RouteDecision(
                    mode=ExecutionMode.PLAN,
                    route_type=RouteType.GENERATED_PLAN,
                    source=RouteSource.RULE,
                    planner_id="default-planner",
                    reason_code="RULE",
                ),
                ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                {"default-planner"},
            ),
            (
                RoutePolicyConstraints(allowed_capability_ids=frozenset({"finance.query/v1"})),
                RouteDecision(
                    mode=ExecutionMode.FAST,
                    route_type=RouteType.DIRECT_CAPABILITY,
                    source=RouteSource.RULE,
                    capability_id="echo.reply/v1",
                    reason_code="RULE",
                ),
                ErrorCode.ROUTE_CAPABILITY_NOT_ALLOWED,
                set(),
            ),
            (
                RoutePolicyConstraints(allowed_planner_ids=frozenset({"other-planner"})),
                RouteDecision(
                    mode=ExecutionMode.PLAN,
                    route_type=RouteType.GENERATED_PLAN,
                    source=RouteSource.RULE,
                    planner_id="default-planner",
                    reason_code="RULE",
                ),
                ErrorCode.ROUTE_PLANNER_NOT_ALLOWED,
                {"default-planner"},
            ),
        )

        for constraints, decision, expected_code, planner_ids in cases:
            with self.subTest(code=expected_code):
                context = make_context(
                    mode=ExecutionMode.AUTO,
                    target=("echo.reply/v1" if decision.mode is ExecutionMode.FAST else None),
                    constraints=constraints,
                )
                with self.assertRaises(RoutingError) as raised:
                    RouteDecisionValidator(planner_ids=planner_ids).validate(decision, context)
                self.assertEqual(raised.exception.code, expected_code)


class RoutingDependencyBoundaryTests(unittest.TestCase):
    def test_routing_foundation_has_no_execution_or_provider_instance_dependency(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "harness_routing"
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(source_root.glob("*.py"))
        )

        for forbidden in (
            "harness_runtime",
            "harness_execution",
            "CapabilityInvoker",
            "ExecutionEngine",
            "ProviderRegistration",
            "ModelGateway",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
