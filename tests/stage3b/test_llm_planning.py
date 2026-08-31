"""LLMPlanner、bounded repair、Plan guard 与 HybridPlanner Acceptance Gate。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import ExecutionMode, ExecutionPlan, PlanNode, ResultStatus
from harness_events import ExecutionEventName
from harness_model import ModelGateway
from harness_planning import HybridPlanner, LLMPlanner, PlanValidator, StaticPlanner
from harness_policy import Policy, PolicyContext, PolicyDecision, PolicyPhase
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_trace import InMemoryTracer, SpanType

from .support import (
    ECHO_TOOL_ID,
    PLAN_MODEL_ID,
    SECOND_TOOL_ID,
    AcceptancePlugin,
    CountingStateStore,
    RecordingTool,
    ScriptedModel,
    echo_plan,
    make_request,
    valid_plan_draft,
)


class PlanningConstraintsPolicy(Policy):
    def __init__(self, **constraints: object) -> None:
        self.constraints = constraints

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_ROUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.allow(
            self.name,
            reason="stage3b planning limits",
            constraints=self.constraints,
        )


def planning_fixture(
    outcomes,
    *,
    policies: tuple[Policy, ...] | None = None,
    state_store=None,
    planner_factory=None,
):
    registry = InMemoryCapabilityRegistry()
    tracer = InMemoryTracer()
    model = ScriptedModel(PLAN_MODEL_ID, outcomes)
    first_tool = RecordingTool()
    second_tool = RecordingTool(SECOND_TOOL_ID)
    registry.register(model, plugin_id="stage3b-plan-model")
    catalog = RegistryCapabilityCatalog(registry)
    validator = PlanValidator(catalog)
    llm_planner = LLMPlanner(
        ModelGateway(registry, tracer),
        planner_model_capability_id=PLAN_MODEL_ID,
        validator=validator,
        planner_id="stage3b-llm-planner",
        plan_id_factory=lambda: "stage3b-generated-plan",
        allowed_capability_ids=(ECHO_TOOL_ID, SECOND_TOOL_ID),
    )
    planner = planner_factory(llm_planner, validator) if planner_factory else llm_planner
    app = build_harness(
        registry=registry,
        tracer=tracer,
        capability_catalog=catalog,
        plan_validator=validator,
        plugins=(AcceptancePlugin((first_tool, second_tool)),),
        planners=(planner,),
        default_planner_id=planner.planner_id,
        policies=policies,
        state_store=state_store,
        entry_point_group=None,
    )
    return app, tracer, model, first_tool, second_tool, llm_planner


class LLMPlanningAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_first_attempt_executes_only_after_planning(self) -> None:
        app, tracer, model, tool, second, _planner = planning_fixture(valid_plan_draft())
        await app.start()
        try:
            result = await app.handle(make_request(mode=ExecutionMode.PLAN))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["execution_mode"], "plan")
        self.assertEqual(result.metadata["planner_id"], "stage3b-llm-planner")
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(second.calls, 0)
        spans = tracer.spans(trace_id=result.trace_id)
        self.assertEqual(sum(span.type is SpanType.REQUEST for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.ROUTE for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.PLANNER for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.MODEL for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.PLAN for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.TOOL for span in spans), 1)

    async def test_invalid_then_repair_then_valid_executes_once(self) -> None:
        invalid = {
            **valid_plan_draft(),
            "outputs": {
                "missing": {
                    "kind": "node_output",
                    "node_id": "does-not-exist",
                    "pointer": "/output/data",
                }
            },
        }
        app, tracer, model, tool, second, _planner = planning_fixture((invalid, valid_plan_draft()))
        await app.start()
        try:
            result = await app.handle(make_request(mode=ExecutionMode.PLAN))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(model.calls, 2)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(second.calls, 0)
        self.assertEqual(
            sum(span.type is SpanType.MODEL for span in tracer.spans(trace_id=result.trace_id)),
            2,
        )
        planner_events = [
            event.name
            for event in app.event_publisher.events()
            if event.name.value.startswith("planner.")
        ]
        self.assertEqual(
            planner_events,
            [
                ExecutionEventName.PLANNER_STARTED,
                ExecutionEventName.PLANNER_REPAIRING,
                ExecutionEventName.PLANNER_COMPLETED,
            ],
        )

    async def test_repair_exhaustion_creates_no_checkpoint_or_business_call(self) -> None:
        invalid = {
            **valid_plan_draft(),
            "outputs": {
                "missing": {
                    "kind": "node_output",
                    "node_id": "does-not-exist",
                    "pointer": "/output/data",
                }
            },
        }
        store = CountingStateStore()
        policy = PlanningConstraintsPolicy(max_plan_attempts=3)
        app, tracer, model, tool, second, _planner = planning_fixture(
            invalid,
            policies=(policy,),
            state_store=store,
        )
        await app.start()
        try:
            result = await app.handle(make_request(mode=ExecutionMode.PLAN))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLANNER.REPAIR_EXHAUSTED")
        self.assertEqual(model.calls, 3)
        self.assertEqual(tool.calls + second.calls, 0)
        self.assertEqual(store.create_calls, 0)
        self.assertEqual(store.save_calls, 0)
        self.assertEqual(
            sum(span.type is SpanType.PLAN for span in tracer.spans(trace_id=result.trace_id)),
            0,
        )
        self.assertIn(
            ExecutionEventName.PLANNER_FAILED,
            [event.name for event in app.event_publisher.events()],
        )

    async def test_unknown_cycle_and_oversized_plans_fail_before_execution(self) -> None:
        unknown = {
            "nodes": [{"node_id": "unknown", "capability_id": "missing.tool/v1"}],
            "edges": [],
            "outputs": {},
        }
        cycle = {
            "nodes": [
                {"node_id": "one", "capability_id": ECHO_TOOL_ID},
                {"node_id": "two", "capability_id": SECOND_TOOL_ID},
            ],
            "edges": [
                {"from_node": "one", "to_node": "two"},
                {"from_node": "two", "to_node": "one"},
            ],
            "outputs": {},
        }
        oversized = {
            "nodes": [
                {"node_id": "one", "capability_id": ECHO_TOOL_ID},
                {"node_id": "two", "capability_id": SECOND_TOOL_ID},
            ],
            "edges": [],
            "outputs": {},
        }
        cases = (
            (unknown, {}, "PLANNING.CAPABILITY_NOT_ALLOWED"),
            (cycle, {}, "PLAN.CYCLE"),
            (oversized, {"max_plan_nodes": 1}, "HARNESS.PLANNER.PLAN_TOO_LARGE"),
        )
        for draft, extra_constraints, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                store = CountingStateStore()
                policy = PlanningConstraintsPolicy(
                    max_plan_attempts=1,
                    **extra_constraints,
                )
                app, _tracer, model, tool, second, _planner = planning_fixture(
                    draft,
                    policies=(policy,),
                    state_store=store,
                )
                await app.start()
                try:
                    result = await app.handle(make_request(mode=ExecutionMode.PLAN))
                finally:
                    await app.shutdown()

                self.assertEqual(result.status, ResultStatus.FAILED)
                self.assertEqual(result.error.code, "HARNESS.PLANNER.REPAIR_EXHAUSTED")
                self.assertIn(expected_code, result.error.details["validation_codes"])
                self.assertEqual(model.calls, 1)
                self.assertEqual(tool.calls + second.calls, 0)
                self.assertEqual(store.create_calls, 0)


class HybridPlannerAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_hit_skips_llm_and_not_applicable_uses_it_once(self) -> None:
        def hybrid_factory(llm_planner, validator):
            primary = StaticPlanner(
                "stage3b-static",
                {"stage3b-goal": echo_plan("stage3b-static-plan")},
                validator=validator,
            )
            return HybridPlanner(
                "stage3b-hybrid-planner",
                primary,
                llm_planner,
                validator=validator,
            )

        app, _tracer, model, tool, second, _planner = planning_fixture(
            valid_plan_draft(),
            planner_factory=hybrid_factory,
        )
        await app.start()
        try:
            deterministic = await app.handle(
                make_request(mode=ExecutionMode.PLAN, request_id="stage3b-static-hit")
            )
            fallback = await app.handle(
                make_request(
                    mode=ExecutionMode.PLAN,
                    input_type="stage3b-other-goal",
                    request_id="stage3b-llm-fallback",
                )
            )
        finally:
            await app.shutdown()

        self.assertEqual(deterministic.status, ResultStatus.SUCCESS)
        self.assertEqual(model.calls, 1)
        self.assertEqual(fallback.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 2)
        self.assertEqual(second.calls, 0)

    async def test_invalid_primary_never_falls_back_to_llm(self) -> None:
        def hybrid_factory(llm_planner, validator):
            invalid = ExecutionPlan(
                plan_id="stage3b-invalid-static-plan",
                nodes=(PlanNode(node_id="bad", capability="missing.tool/v1"),),
            )
            primary = StaticPlanner(
                "stage3b-invalid-static",
                {"stage3b-goal": invalid},
                validator=validator,
            )
            return HybridPlanner(
                "stage3b-hybrid-invalid",
                primary,
                llm_planner,
                validator=validator,
            )

        app, _tracer, model, tool, second, _planner = planning_fixture(
            valid_plan_draft(),
            planner_factory=hybrid_factory,
        )
        await app.start()
        try:
            result = await app.handle(make_request(mode=ExecutionMode.PLAN))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLANNER.INVALID_OUTPUT")
        self.assertEqual(model.calls, 0)
        self.assertEqual(tool.calls + second.calls, 0)


if __name__ == "__main__":
    unittest.main()
