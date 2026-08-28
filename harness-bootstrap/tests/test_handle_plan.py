"""Stage 3B Step 9 ``handle()`` PLAN 共享生命周期集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from harness_bootstrap import build_harness
from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    CapabilityDescriptor,
    CapabilityType,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    NodeOutputBinding,
    PlanEdge,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    RequestInput,
    RequestOptions,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RouteDecision,
    RouteSource,
    RouteType,
)
from harness_planning import Planner, PlanningContext
from harness_policy import Policy, PolicyContext, PolicyDecision, PolicyPhase
from harness_routing import Router, RoutingContext
from harness_runtime import InvocationContextFactory
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_state import SQLiteStateStore
from harness_trace import InMemoryTracer, SpanStatus, SpanType

TOOL_ID = "plan.echo/v1"


class RecordingTool(ToolSPI):
    def __init__(self) -> None:
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=TOOL_ID,
            name="Plan echo",
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.contexts.append(context)
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data=request.model_dump(mode="json")["arguments"],
            )
        )


class ToolPlugin(PluginSPI):
    def __init__(self, tool: RecordingTool) -> None:
        self.tool = tool

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="plan-tools",
            name="Plan tools",
            version="1.0.0",
            sdk_version="1",
            capabilities=(TOOL_ID,),
        )

    def capabilities(self) -> tuple[ToolSPI, ...]:
        return (self.tool,)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class RecordingPlanner(Planner):
    def __init__(
        self,
        plan: ExecutionPlan,
        *,
        planner_id: str = "recording-planner",
        fail_if_called: bool = False,
    ) -> None:
        self._plan = plan
        self._planner_id = planner_id
        self._fail_if_called = fail_if_called
        self.contexts: list[PlanningContext] = []

    @property
    def planner_id(self) -> str:
        return self._planner_id

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        if self._fail_if_called:
            raise AssertionError("persisted plan must resume without calling Planner")
        self.contexts.append(context)
        return self._plan


class RecordingContextFactory(InvocationContextFactory):
    def __init__(self, deadline_at: datetime) -> None:
        self.deadline_at = deadline_at
        self.requests: list[Request] = []

    def create(self, request: Request) -> InvocationContext:
        self.requests.append(request)
        return InvocationContext(request=request, deadline_at=self.deadline_at)


class RecordingPlanRouter(Router):
    def __init__(self, *, fail_if_called: bool = False) -> None:
        self.fail_if_called = fail_if_called
        self.contexts: list[RoutingContext] = []

    @property
    def router_id(self) -> str:
        return "recording-plan-router"

    async def route(self, context: RoutingContext) -> RouteDecision:
        if self.fail_if_called:
            raise AssertionError("persisted plan must resume without calling Router")
        self.contexts.append(context)
        return RouteDecision(
            mode=ExecutionMode.PLAN,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.RULE,
            confidence=1.0,
            reason_code="TEST_PLAN",
        )


class PlanningPolicy(Policy):
    def __init__(self, *, deny_pre_plan: bool = False) -> None:
        self.deny_pre_plan = deny_pre_plan
        self.calls: list[PolicyPhase] = []

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_ROUTE, PolicyPhase.PRE_PLAN, PolicyPhase.PRE_EXECUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        self.calls.append(context.phase)
        if self.deny_pre_plan and context.phase is PolicyPhase.PRE_PLAN:
            return PolicyDecision.deny(self.name, reason="plan denied by test policy")
        if context.phase is PolicyPhase.PRE_ROUTE:
            return PolicyDecision.allow(
                self.name,
                reason="constrain planning",
                constraints={
                    "allowed_planner_ids": ["recording-planner"],
                    "allowed_capability_ids": [TOOL_ID],
                    "max_plan_attempts": 2,
                    "max_plan_nodes": 4,
                },
            )
        return PolicyDecision.allow(self.name, reason="allowed")


def plan_request(*, trace: bool = True) -> Request:
    return Request(
        request_id="handle-plan-request",
        input=RequestInput(type="goal", content={"message": "hello"}),
        options=RequestOptions(execution_mode=ExecutionMode.PLAN, trace=trace),
    )


def echo_plan(plan_id: str = "handle-plan-success") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        nodes=(
            PlanNode(
                node_id="echo",
                capability=TOOL_ID,
                input_mapping={"message": RequestBinding(pointer="/input/content/message")},
            ),
        ),
        outputs={
            "message": NodeOutputBinding(
                node_id="echo",
                pointer="/output/data/message",
            )
        },
    )


def approval_plan(plan_id: str = "handle-plan-waiting") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        nodes=(
            PlanNode(node_id="approve", kind=PlanNodeKind.APPROVAL),
            PlanNode(
                node_id="echo",
                capability=TOOL_ID,
                input_mapping={"message": RequestBinding(pointer="/input/content/message")},
            ),
        ),
        edges=(PlanEdge(from_node="approve", to_node="echo"),),
        outputs={
            "message": NodeOutputBinding(
                node_id="echo",
                pointer="/output/data/message",
            )
        },
    )


class HandlePlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_reuses_context_deadline_trace_and_policy_boundaries(self) -> None:
        deadline = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
        context_factory = RecordingContextFactory(deadline)
        tracer = InMemoryTracer()
        policy = PlanningPolicy()
        planner = RecordingPlanner(echo_plan())
        tool = RecordingTool()
        app = build_harness(
            plugins=(ToolPlugin(tool),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            policies=(policy,),
            context_factory=context_factory,
            tracer=tracer,
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(plan_request())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(result.metadata["execution_mode"], "plan")
        self.assertEqual(result.metadata["planner_id"], planner.planner_id)
        self.assertEqual(len(context_factory.requests), 1)
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(len(tool.contexts), 1)
        planning = planner.contexts[0]
        self.assertEqual(planning.invocation.deadline_at, deadline)
        self.assertEqual(tool.contexts[0].deadline_at, deadline)
        self.assertEqual(planning.constraints.deadline_at, deadline)
        self.assertEqual(planning.constraints.max_plan_attempts, 2)
        self.assertEqual(planning.constraints.max_plan_nodes, 4)
        self.assertEqual(planning.constraints.allowed_capability_ids, frozenset({TOOL_ID}))
        self.assertIn(PolicyPhase.PRE_PLAN, policy.calls)
        self.assertIn(PolicyPhase.PRE_EXECUTE, policy.calls)

        spans = tracer.spans(trace_id=result.trace_id)
        request_spans = [span for span in spans if span.type is SpanType.REQUEST]
        runtime_spans = [span for span in spans if span.name == "runtime.handle"]
        planner_spans = [span for span in spans if span.type is SpanType.PLANNER]
        plan_spans = [span for span in spans if span.type is SpanType.PLAN]
        self.assertEqual(len(request_spans), 1)
        self.assertEqual(len(runtime_spans), 1)
        self.assertEqual(len(planner_spans), 1)
        self.assertEqual(len(plan_spans), 1)
        self.assertEqual(runtime_spans[0].parent_span_id, request_spans[0].span_id)
        self.assertEqual(planner_spans[0].parent_span_id, runtime_spans[0].span_id)
        self.assertEqual(plan_spans[0].parent_span_id, runtime_spans[0].span_id)
        self.assertTrue(all(span.status is not SpanStatus.RUNNING for span in spans))
        await app.shutdown()

    async def test_pre_plan_deny_stops_capability_after_one_planner_call(self) -> None:
        policy = PlanningPolicy(deny_pre_plan=True)
        planner = RecordingPlanner(echo_plan("handle-plan-denied"))
        tool = RecordingTool()
        app = build_harness(
            plugins=(ToolPlugin(tool),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            policies=(policy,),
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(plan_request(trace=False))

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(result.error.code, "HARNESS.POLICY.PLAN_DENIED")
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(tool.contexts, [])
        await app.shutdown()

    async def test_invalid_planner_output_never_reaches_execution(self) -> None:
        invalid = ExecutionPlan(
            plan_id="handle-plan-invalid",
            nodes=(PlanNode(node_id="unknown", capability="missing.tool/v1"),),
        )
        planner = RecordingPlanner(invalid)
        tool = RecordingTool()
        tracer = InMemoryTracer()
        app = build_harness(
            plugins=(ToolPlugin(tool),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            tracer=tracer,
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(plan_request())

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.INVALID")
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(tool.contexts, [])
        spans = tracer.spans(trace_id=result.trace_id)
        self.assertEqual(sum(span.type is SpanType.PLAN for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.PLANNER for span in spans), 1)
        await app.shutdown()

    async def test_waiting_and_cross_application_resume_do_not_replan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "handle-plan.db"
            plan = approval_plan("handle-plan-restart")
            first_planner = RecordingPlanner(plan)
            first_router = RecordingPlanRouter()
            first_tool = RecordingTool()
            first_app = build_harness(
                plugins=(ToolPlugin(first_tool),),
                planners=(first_planner,),
                default_planner_id=first_planner.planner_id,
                router=first_router,
                state_store=SQLiteStateStore(database),
                entry_point_group=None,
            )
            await first_app.start()

            waiting = await first_app.handle(plan_request(trace=False))
            saved = await first_app.state_store.load(plan.plan_id)

            self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
            self.assertEqual(len(first_router.contexts), 1)
            self.assertEqual(len(first_planner.contexts), 1)
            self.assertEqual(first_tool.contexts, [])
            self.assertEqual(saved.plan, plan)
            self.assertEqual(saved.context.request, plan_request(trace=False))
            self.assertNotIn("route_decision", saved.model_dump(mode="json"))
            self.assertNotIn("planning_attempt", saved.model_dump(mode="json"))
            await first_app.shutdown()

            resumed_planner = RecordingPlanner(plan, fail_if_called=True)
            resumed_router = RecordingPlanRouter(fail_if_called=True)
            resumed_tool = RecordingTool()
            resumed_app = build_harness(
                plugins=(ToolPlugin(resumed_tool),),
                planners=(resumed_planner,),
                default_planner_id=resumed_planner.planner_id,
                router=resumed_router,
                state_store=SQLiteStateStore(database),
                entry_point_group=None,
            )
            await resumed_app.start()

            resumed = await resumed_app.resume_plan(plan.plan_id)
            completed = await resumed_app.resolve_approval(
                plan.plan_id,
                ApprovalDecision(
                    approval_id=resumed.continuation.approval_id,
                    decision=ApprovalDecisionType.APPROVED,
                    decided_by="stage3b-test",
                ),
            )

            self.assertEqual(resumed.status, ResultStatus.ACCEPTED)
            self.assertEqual(completed.status, ResultStatus.SUCCESS)
            self.assertEqual(completed.output.data["message"], "hello")
            self.assertEqual(resumed_router.contexts, [])
            self.assertEqual(resumed_planner.contexts, [])
            self.assertEqual(len(resumed_tool.contexts), 1)
            await resumed_app.shutdown()


if __name__ == "__main__":
    unittest.main()
