"""统一 handle 生命周期、Trace/Deadline 传播与 WAITING/Resume Gate。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from harness_bootstrap import build_harness
from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ExecutionMode,
    ResultStatus,
)
from harness_model import ModelGateway
from harness_planning import LLMPlanner, PlanValidator
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_routing import LLMRouter, RouteDecisionValidator, RuleRouter
from harness_state import SQLiteStateStore
from harness_trace import InMemoryTracer, SpanStatus, SpanType

from .support import (
    ECHO_TOOL_ID,
    PLAN_MODEL_ID,
    ROUTE_MODEL_ID,
    AcceptancePlugin,
    RecordingContextFactory,
    RecordingTool,
    ScriptedModel,
    ScriptedPlanner,
    ScriptedRouter,
    echo_plan,
    make_request,
    plan_decision,
)


def approval_draft() -> dict[str, object]:
    return {
        "nodes": [
            {"node_id": "approval", "kind": "approval"},
            {
                "node_id": "echo",
                "capability": ECHO_TOOL_ID,
                "input_mapping": {
                    "message": {
                        "kind": "request",
                        "pointer": "/input/content/message",
                    }
                },
            },
        ],
        "edges": [{"from_node": "approval", "to_node": "echo"}],
        "outputs": {
            "message": {
                "kind": "node_output",
                "node_id": "echo",
                "pointer": "/output/data/message",
            }
        },
    }


def llm_waiting_app(
    database: Path,
    *,
    route_model: ScriptedModel,
    plan_model: ScriptedModel,
    tool: RecordingTool,
):
    registry = InMemoryCapabilityRegistry()
    tracer = InMemoryTracer()
    registry.register(route_model, plugin_id="stage3b-route-model")
    registry.register(plan_model, plugin_id="stage3b-plan-model")
    catalog = RegistryCapabilityCatalog(registry)
    validator = PlanValidator(catalog)
    gateway = ModelGateway(registry, tracer)
    router = RuleRouter(
        fallback=LLMRouter(
            gateway,
            route_model_capability_id=ROUTE_MODEL_ID,
            decision_validator=RouteDecisionValidator(),
        )
    )
    planner = LLMPlanner(
        gateway,
        planner_model_capability_id=PLAN_MODEL_ID,
        validator=validator,
        planner_id="stage3b-resume-planner",
        plan_id_factory=lambda: "stage3b-resume-plan",
        allowed_capability_ids=(ECHO_TOOL_ID,),
    )
    app = build_harness(
        registry=registry,
        tracer=tracer,
        capability_catalog=catalog,
        plan_validator=validator,
        state_store=SQLiteStateStore(database),
        router=router,
        planners=(planner,),
        default_planner_id=planner.planner_id,
        plugins=(AcceptancePlugin((tool,)),),
        entry_point_group=None,
    )
    return app, tracer


class HandleLifecycleAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_reuses_one_context_deadline_and_trace(self) -> None:
        deadline = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
        context_factory = RecordingContextFactory(deadline)
        tracer = InMemoryTracer()
        tool = RecordingTool()
        planner = ScriptedPlanner(echo_plan())
        router = ScriptedRouter(plan_decision())
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            router=router,
            planners=(planner,),
            default_planner_id=planner.planner_id,
            context_factory=context_factory,
            tracer=tracer,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request(mode=ExecutionMode.PLAN))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(len(context_factory.requests), 1)
        self.assertEqual(len(router.contexts), 1)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(router.contexts[0].invocation.deadline_at, deadline)
        self.assertEqual(planner.contexts[0].invocation.deadline_at, deadline)
        self.assertEqual(planner.contexts[0].constraints.deadline_at, deadline)
        self.assertEqual(tool.contexts[0].deadline_at, deadline)

        spans = tracer.spans(trace_id=result.trace_id)
        for span_type in (
            SpanType.REQUEST,
            SpanType.RUNTIME,
            SpanType.ROUTE,
            SpanType.PLANNER,
            SpanType.PLAN,
            SpanType.TOOL,
        ):
            with self.subTest(span_type=span_type):
                self.assertTrue(any(span.type is span_type for span in spans))
        self.assertTrue(all(span.trace_id == result.trace_id for span in spans))
        self.assertTrue(all(span.status is not SpanStatus.RUNNING for span in spans))

    async def test_waiting_and_cross_application_resume_never_reroute_or_replan(self) -> None:
        route_output = {
            "mode": "plan",
            "route_type": "generated_plan",
            "source": "model",
            "confidence": 0.9,
            "reason_code": "STAGE3B_MODEL_PLAN",
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stage3b-resume.db"
            first_route_model = ScriptedModel(ROUTE_MODEL_ID, route_output)
            first_plan_model = ScriptedModel(PLAN_MODEL_ID, approval_draft())
            first_tool = RecordingTool()
            first_app, _first_tracer = llm_waiting_app(
                database,
                route_model=first_route_model,
                plan_model=first_plan_model,
                tool=first_tool,
            )
            await first_app.start()
            waiting = await first_app.handle(make_request(trace=False))
            saved = await first_app.state_store.load("stage3b-resume-plan")
            await first_app.shutdown()

            self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
            self.assertEqual(first_route_model.calls, 1)
            self.assertEqual(first_plan_model.calls, 1)
            self.assertEqual(first_tool.calls, 0)
            self.assertIsNotNone(saved)

            poison = {"unexpected": "model must not run during resume"}
            resumed_route_model = ScriptedModel(ROUTE_MODEL_ID, poison)
            resumed_plan_model = ScriptedModel(PLAN_MODEL_ID, poison)
            resumed_tool = RecordingTool()
            resumed_app, _resumed_tracer = llm_waiting_app(
                database,
                route_model=resumed_route_model,
                plan_model=resumed_plan_model,
                tool=resumed_tool,
            )
            await resumed_app.start()
            try:
                resumed = await resumed_app.resume_plan("stage3b-resume-plan")
                completed = await resumed_app.resolve_approval(
                    "stage3b-resume-plan",
                    ApprovalDecision(
                        approval_id=resumed.continuation.approval_id,
                        decision=ApprovalDecisionType.APPROVED,
                        decided_by="stage3b-acceptance",
                    ),
                )
            finally:
                await resumed_app.shutdown()

            self.assertEqual(resumed.status, ResultStatus.ACCEPTED)
            self.assertEqual(completed.status, ResultStatus.SUCCESS)
            self.assertEqual(completed.output.data["message"], "stage3b-secret-goal")
            self.assertEqual(resumed_route_model.calls, 0)
            self.assertEqual(resumed_plan_model.calls, 0)
            self.assertEqual(resumed_tool.calls, 1)


if __name__ == "__main__":
    unittest.main()
