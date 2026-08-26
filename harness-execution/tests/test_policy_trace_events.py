"""Stage 2 Step 10 Policy / Trace / Execution Events 集成测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    Continuation,
    EgressType,
    ExecutionPlan,
    InvocationContext,
    NodeOutputBinding,
    PlanNode,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    SideEffectType,
)
from harness_events import EventSubscriber, ExecutionEvent, ExecutionEventName, InMemoryEventBus
from harness_execution import BasicScheduler, ExecutionEngine
from harness_planning import PlanValidator
from harness_policy import (
    AllowAllPolicy,
    Policy,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyPhase,
    RequireApprovalPolicy,
)
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
from harness_state import InMemoryStateStore
from harness_trace import InMemoryTracer, SpanType


class GovernedTool(ToolSPI):
    def __init__(
        self,
        capability_id: str = "governed.tool/v1",
        *,
        async_job: bool = False,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                egress=EgressType.EXTERNAL,
            ),
        )
        self.async_job = async_job
        self.calls = 0
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.contexts.append(context)
        if self.async_job:
            return ResultEnvelope.accepted(
                Continuation(job_ref="job-step10", waiting_reason="external_job")
            )
        arguments = request.model_dump(mode="json")["arguments"]
        return ResultEnvelope.success(ResultOutput(type="json", data=arguments))


class DenyPrePlanPolicy(Policy):
    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_PLAN})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.deny(self.name, reason="plan blocked by governance")


class FailingSubscriber(EventSubscriber):
    async def on_event(self, event: ExecutionEvent) -> None:
        raise RuntimeError("observer unavailable")


def make_engine(
    provider: ToolSPI,
    *,
    policies: tuple[Policy, ...],
    event_bus: InMemoryEventBus | None = None,
) -> tuple[ExecutionEngine, InMemoryTracer, InMemoryStateStore, InMemoryEventBus]:
    registry = InMemoryCapabilityRegistry()
    registry.register(provider, plugin_id="step10-tests")
    tracer = InMemoryTracer()
    lifecycle = InvocationLifecycle(
        tracer,
        context_factory=DefaultInvocationContextFactory(),
    )
    invoker = CapabilityInvoker(
        registry,
        PolicyEngine(policies),
        tracer,
        lifecycle=lifecycle,
    )
    validator = PlanValidator(RegistryCapabilityCatalog(registry))
    scheduler = BasicScheduler(invoker, tracer, lifecycle)
    store = InMemoryStateStore()
    bus = event_bus or InMemoryEventBus()
    engine = ExecutionEngine(
        validator,
        scheduler,
        invoker,
        tracer,
        lifecycle,
        state_store=store,
        event_publisher=bus,
    )
    return engine, tracer, store, bus


def make_request() -> Request:
    return Request(
        request_id="step10-request",
        input=RequestInput(
            type="json",
            content={"account": "A-1", "secret": "must-never-enter-approval-summary"},
        ),
    )


def make_plan(capability_id: str = "governed.tool/v1") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="step10-plan",
        nodes=(
            PlanNode(
                node_id="work",
                capability=capability_id,
                input_mapping={
                    "account": RequestBinding(pointer="/input/content/account"),
                    "secret": RequestBinding(pointer="/input/content/secret"),
                },
            ),
        ),
        outputs={
            "account": NodeOutputBinding(
                node_id="work",
                pointer="/output/data/account",
            )
        },
    )


class PolicyTraceEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_plan_deny_stops_before_scheduler_and_provider(self) -> None:
        tool = GovernedTool()
        engine, tracer, store, _ = make_engine(
            tool,
            policies=(AllowAllPolicy(), DenyPrePlanPolicy()),
        )

        result = await engine.execute(make_request(), make_plan())

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(result.error.code, "HARNESS.POLICY.PLAN_DENIED")
        self.assertEqual(tool.calls, 0)
        self.assertIsNone(await store.load("step10-plan"))
        policy_spans = [span for span in tracer.spans() if span.name == "policy.pre_plan"]
        self.assertEqual(len(policy_spans), 1)
        self.assertEqual(policy_spans[0].type, SpanType.POLICY)

    async def test_policy_approval_grant_reenters_pre_execute_without_loop(self) -> None:
        tool = GovernedTool()
        engine, _, store, bus = make_engine(
            tool,
            policies=(
                AllowAllPolicy(),
                RequireApprovalPolicy(capabilities=("governed.tool/v1",)),
            ),
        )
        waiting = await engine.execute(make_request(), make_plan())
        saved_waiting = await store.load("step10-plan")

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(tool.calls, 0)
        self.assertIsNotNone(waiting.continuation.approval_id)
        request = saved_waiting.state.pending_approvals[0]
        self.assertEqual(request.capability, "governed.tool/v1")
        self.assertEqual(request.side_effect, SideEffectType.WRITE)
        self.assertEqual(request.egress, EgressType.EXTERNAL)
        self.assertEqual(
            request.model_dump(mode="json")["parameter_summary"],
            {"parameter_names": ["account", "secret"]},
        )
        self.assertNotIn("must-never-enter-approval-summary", request.model_dump_json())

        result = await engine.resolve_approval(
            "step10-plan",
            ApprovalDecision(
                approval_id=waiting.continuation.approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer-step10",
            ),
        )
        saved = await store.load("step10-plan")

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(len(saved.state.metadata["approval_grants"]), 1)
        self.assertNotIn("_harness_approval_grants", saved.context.attributes)
        grant_payload = tool.contexts[0].attributes["_harness_approval_grants"]
        self.assertEqual(grant_payload[0]["approval_id"], waiting.continuation.approval_id)
        names = [event.name for event in bus.events()]
        self.assertIn(ExecutionEventName.APPROVAL_REQUESTED, names)
        self.assertIn(ExecutionEventName.APPROVAL_RESOLVED, names)
        self.assertIn(ExecutionEventName.PLAN_RESUMED, names)
        self.assertIn(ExecutionEventName.NODE_RESUMED, names)

    async def test_trace_contains_scheduler_and_checkpoint_events(self) -> None:
        tool = GovernedTool()
        engine, tracer, _, bus = make_engine(tool, policies=(AllowAllPolicy(),))

        result = await engine.execute(make_request(), make_plan())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        spans = tracer.spans(trace_id=result.trace_id)
        plan_span = next(span for span in spans if span.type is SpanType.PLAN)
        scheduler_span = next(span for span in spans if span.type is SpanType.SCHEDULER)
        node_span = next(span for span in spans if span.type is SpanType.PLAN_NODE)
        self.assertEqual(scheduler_span.parent_span_id, plan_span.span_id)
        self.assertEqual(node_span.parent_span_id, plan_span.span_id)

        trace_event_names = {event.name for event in tracer.events(trace_id=result.trace_id)}
        self.assertIn(ExecutionEventName.NODE_STARTED.value, trace_event_names)
        self.assertIn(ExecutionEventName.NODE_COMPLETED.value, trace_event_names)
        self.assertIn(ExecutionEventName.CHECKPOINT_SAVED.value, trace_event_names)

        event_names = [event.name for event in bus.events()]
        self.assertIn(ExecutionEventName.PLAN_CREATED, event_names)
        self.assertIn(ExecutionEventName.PLAN_STARTED, event_names)
        self.assertIn(ExecutionEventName.NODE_READY, event_names)
        self.assertIn(ExecutionEventName.NODE_STARTED, event_names)
        self.assertIn(ExecutionEventName.NODE_COMPLETED, event_names)
        self.assertIn(ExecutionEventName.PLAN_COMPLETED, event_names)
        self.assertIn(ExecutionEventName.CHECKPOINT_SAVED, event_names)

    async def test_event_subscriber_failure_does_not_fail_execution(self) -> None:
        tool = GovernedTool()
        bus = InMemoryEventBus()
        bus.subscribe(FailingSubscriber())
        engine, _, _, _ = make_engine(
            tool,
            policies=(AllowAllPolicy(),),
            event_bus=bus,
        )

        result = await engine.execute(make_request(), make_plan())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 1)
        self.assertGreater(len(bus.events()), 0)

    async def test_async_waiting_emits_accepted_completed_and_resume(self) -> None:
        tool = GovernedTool(async_job=True)
        engine, _, _, bus = make_engine(tool, policies=(AllowAllPolicy(),))
        plan = ExecutionPlan(
            plan_id="step10-async",
            nodes=(PlanNode(node_id="work", capability="governed.tool/v1"),),
        )

        waiting = await engine.execute(make_request(), plan)
        result = await engine.complete_async_node(
            plan.plan_id,
            "work",
            ResultEnvelope.success(ResultOutput(type="json", data={"done": True})),
        )

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        names = [event.name for event in bus.events()]
        self.assertIn(ExecutionEventName.ASYNC_ACCEPTED, names)
        self.assertIn(ExecutionEventName.ASYNC_COMPLETED, names)
        self.assertIn(ExecutionEventName.PLAN_RESUMED, names)
