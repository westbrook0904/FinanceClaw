"""Stage 3B Planner SPI、Registry、Static 与 Hybrid 行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionPlan,
    HarnessTimeoutError,
    InvocationContext,
    PlannerNotApplicableError,
    PlanningError,
    PlanNode,
    PolicyError,
    Request,
    RequestInput,
    ResultEnvelope,
)
from harness_planning import (
    HybridPlanner,
    Planner,
    PlannerRegistry,
    PlanningConstraints,
    PlanningContext,
    PlanValidator,
    StaticPlanner,
)
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_routing import SafeRequestProjector
from harness_spi import ToolRequest, ToolSPI
from pydantic import ValidationError


class StubTool(ToolSPI):
    def __init__(self, capability_id: str) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise NotImplementedError


class ScriptedPlanner(Planner):
    def __init__(self, planner_id: str, outcome: object) -> None:
        self._planner_id = planner_id
        self.outcome = outcome
        self.calls = 0

    @property
    def planner_id(self) -> str:
        return self._planner_id

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


def make_catalog(
    *capability_ids: str,
) -> tuple[RegistryCapabilityCatalog, tuple[CapabilityDescriptor, ...]]:
    registry = InMemoryCapabilityRegistry()
    for capability_id in capability_ids:
        registry.register(StubTool(capability_id), plugin_id="test-plugin")
    catalog = RegistryCapabilityCatalog(registry)
    return catalog, catalog.list()


def make_context(*, input_type: str = "analysis") -> PlanningContext:
    request = Request(input=RequestInput(type=input_type, content={"goal": "rank"}))
    invocation = InvocationContext(
        request=request,
        deadline_at=datetime(2030, 1, 2, 3, 4, tzinfo=UTC),
    )
    _, descriptors = make_catalog("data.rank/v1")
    return PlanningContext(
        invocation=invocation,
        goal=SafeRequestProjector().project(request),
        catalog_snapshot=descriptors,
        constraints=PlanningConstraints(
            allowed_capability_ids=frozenset({"data.rank/v1"}),
            deadline_at=invocation.deadline_at,
        ),
    )


def make_plan(
    plan_id: str = "plan-001",
    capability_id: str = "data.rank/v1",
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        nodes=(PlanNode(node_id="rank", capability=capability_id),),
    )


class PlanningContextTests(unittest.TestCase):
    def test_constraints_defaults_and_context_round_trip(self) -> None:
        context = make_context()
        payload = context.model_dump(mode="json")
        restored = PlanningContext.model_validate(payload)

        self.assertEqual(restored, context)
        self.assertEqual(PlanningConstraints().max_plan_attempts, 3)
        self.assertEqual(PlanningConstraints().max_plan_nodes, 32)
        self.assertEqual(restored.catalog_snapshot[0].id, "data.rank/v1")

    def test_context_rejects_mismatched_goal_and_duplicate_catalog(self) -> None:
        context = make_context()
        other = Request(input=RequestInput(type="analysis", content={}))
        mismatched_goal = SafeRequestProjector().project(other)

        with self.assertRaises(ValidationError):
            PlanningContext(
                invocation=context.invocation,
                goal=mismatched_goal,
                catalog_snapshot=context.catalog_snapshot,
            )
        with self.assertRaises(ValidationError):
            PlanningContext(
                invocation=context.invocation,
                goal=context.goal,
                catalog_snapshot=context.catalog_snapshot * 2,
            )

    def test_constraints_reject_naive_deadline(self) -> None:
        with self.assertRaises(ValidationError):
            PlanningConstraints(deadline_at=datetime(2030, 1, 2, 3, 4))


class PlannerRegistryTests(unittest.TestCase):
    def test_registry_is_read_only_and_preserves_build_order(self) -> None:
        first = ScriptedPlanner("first", make_plan("first-plan"))
        second = ScriptedPlanner("second", make_plan("second-plan"))
        registry = PlannerRegistry((first, second))

        self.assertEqual(registry.list(), ("first", "second"))
        self.assertEqual(registry.planner_ids, ("first", "second"))
        self.assertIs(registry.get("first"), first)
        self.assertEqual(len(registry), 2)
        self.assertFalse(hasattr(registry, "register"))

    def test_registry_rejects_duplicate_ids_and_unknown_lookup(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate planner_id"):
            PlannerRegistry(
                (
                    ScriptedPlanner("duplicate", make_plan()),
                    ScriptedPlanner("duplicate", make_plan()),
                )
            )

        registry = PlannerRegistry()
        with self.assertRaises(PlanningError) as raised:
            registry.get("missing")
        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_NOT_CONFIGURED)


class StaticPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_factory_uses_route_key_and_validates_plan(self) -> None:
        catalog, _ = make_catalog("data.rank/v1")
        calls: list[PlanningContext] = []

        async def factory(context: PlanningContext) -> ExecutionPlan:
            calls.append(context)
            return make_plan()

        planner = StaticPlanner(
            "static",
            {"analysis": factory},
            validator=PlanValidator(catalog),
        )
        context = make_context()

        result = await planner.plan(context)

        self.assertNotEqual(result.plan_id, "plan-001")
        self.assertEqual(result.revision, 1)
        self.assertEqual(result.nodes, make_plan().nodes)
        self.assertEqual(calls, [context])
        self.assertEqual(planner.route_keys, ("analysis",))

    async def test_static_template_is_validated_and_missing_key_is_not_applicable(self) -> None:
        catalog, _ = make_catalog("data.rank/v1")
        planner = StaticPlanner(
            "static",
            {"analysis": make_plan(capability_id="missing/v1")},
            validator=PlanValidator(catalog),
        )

        with self.assertRaises(PlanningError) as invalid:
            await planner.plan(make_context())
        self.assertEqual(invalid.exception.code, ErrorCode.PLANNER_INVALID_OUTPUT)
        self.assertEqual(
            invalid.exception.details["validation_codes"],
            ["PLAN.CAPABILITY_NOT_FOUND"],
        )

        with self.assertRaises(PlannerNotApplicableError) as missing:
            await planner.plan(make_context(input_type="unknown"))
        self.assertEqual(missing.exception.code, ErrorCode.PLANNER_NOT_APPLICABLE)


class HybridPlannerTests(unittest.IsolatedAsyncioTestCase):
    def make_hybrid(
        self,
        primary: Planner,
        fallback: Planner,
    ) -> HybridPlanner:
        catalog, _ = make_catalog("data.rank/v1")
        return HybridPlanner(
            "hybrid",
            primary,
            fallback,
            validator=PlanValidator(catalog),
        )

    async def test_primary_hit_keeps_fallback_at_zero_calls(self) -> None:
        primary = ScriptedPlanner("primary", make_plan("primary-plan"))
        fallback = ScriptedPlanner("fallback", make_plan("fallback-plan"))

        result = await self.make_hybrid(primary, fallback).plan(make_context())

        self.assertNotEqual(result.plan_id, "primary-plan")
        self.assertEqual(result.nodes, make_plan("primary-plan").nodes)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    async def test_not_applicable_calls_fallback_exactly_once(self) -> None:
        primary = ScriptedPlanner(
            "primary",
            PlannerNotApplicableError("no deterministic route"),
        )
        fallback = ScriptedPlanner("fallback", make_plan("fallback-plan"))

        result = await self.make_hybrid(primary, fallback).plan(make_context())

        self.assertNotEqual(result.plan_id, "fallback-plan")
        self.assertEqual(result.nodes, make_plan("fallback-plan").nodes)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    async def test_invalid_primary_output_never_falls_back(self) -> None:
        primary = ScriptedPlanner("primary", make_plan(capability_id="missing/v1"))
        fallback = ScriptedPlanner("fallback", make_plan("fallback-plan"))

        with self.assertRaises(PlanningError) as raised:
            await self.make_hybrid(primary, fallback).plan(make_context())

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_INVALID_OUTPUT)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    async def test_denied_and_timeout_primary_never_fall_back(self) -> None:
        failures = (
            PolicyError("planning denied"),
            HarnessTimeoutError("planning deadline exceeded"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                primary = ScriptedPlanner("primary", failure)
                fallback = ScriptedPlanner("fallback", make_plan("fallback-plan"))

                with self.assertRaises(type(failure)):
                    await self.make_hybrid(primary, fallback).plan(make_context())

                self.assertEqual(primary.calls, 1)
                self.assertEqual(fallback.calls, 0)


if __name__ == "__main__":
    unittest.main()
