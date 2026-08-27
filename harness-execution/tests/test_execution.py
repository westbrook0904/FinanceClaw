"""Basic Scheduler 与 ExecutionEngine 行为测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityExecutionProfile,
    CapabilityType,
    ConditionExpr,
    ConditionOperator,
    ExecutionPlan,
    FailurePolicy,
    IdempotencyType,
    InvocationContext,
    LiteralBinding,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanBudget,
    PlanEdge,
    PlanNode,
    ProviderDescriptor,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
    ValueReference,
)
from harness_execution import (
    BasicScheduler,
    CancellationSignal,
    ConditionEvaluator,
    ExecutionEngine,
    resolve_json_pointer,
)
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
from harness_state import SQLiteStateStore, StateStore
from harness_trace import InMemoryTracer, SpanType


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.current = 0
        self.maximum = 0


class ControlledTool(ToolSPI):
    def __init__(
        self,
        capability_id: str,
        calls: list[str],
        *,
        fail: bool = False,
        delay: float = 0,
        tracker: ConcurrencyTracker | None = None,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self._calls = calls
        self._fail = fail
        self._delay = delay
        self._tracker = tracker

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        arguments = request.model_dump(mode="json")["arguments"]
        label = str(arguments.get("label", self._descriptor.id))
        self._calls.append(label)
        if self._tracker is not None:
            self._tracker.current += 1
            self._tracker.maximum = max(
                self._tracker.maximum,
                self._tracker.current,
            )
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._fail:
                return ResultEnvelope.failure(CapabilityError("controlled failure").to_detail())
            return ResultEnvelope.success(ResultOutput(type="json", data=arguments))
        finally:
            if self._tracker is not None:
                self._tracker.current -= 1


class ReliabilityTool(ToolSPI):
    """可控制瞬态失败和阻塞行为的可靠性测试 Tool。"""

    def __init__(
        self,
        capability_id: str,
        *,
        failures_before_success: int = 0,
        profile: CapabilityExecutionProfile | None = None,
        delay: float = 0,
        started: asyncio.Event | None = None,
        fallbackable: bool = False,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self.failures_before_success = failures_before_success
        self.delay = delay
        self.started = started
        self.fallbackable = fallbackable
        self.calls = 0
        self.cancelled = False
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
        if self.started is not None:
            self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.calls <= self.failures_before_success:
            return ResultEnvelope.failure(
                CapabilityError(
                    "transient failure",
                    retryable=True,
                    fallbackable=self.fallbackable,
                ).to_detail()
            )
        return ResultEnvelope.success(ResultOutput(type="json", data={"calls": self.calls}))


def make_engine(
    *providers: ToolSPI,
    state_store: StateStore | None = None,
) -> tuple[ExecutionEngine, InMemoryTracer]:
    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="execution-tests")
    return make_engine_from_registry(registry, state_store=state_store)


def make_engine_from_registry(
    registry: InMemoryCapabilityRegistry,
    *,
    state_store: StateStore | None = None,
) -> tuple[ExecutionEngine, InMemoryTracer]:
    tracer = InMemoryTracer()
    policy_engine = PolicyEngine((AllowAllPolicy(),))
    lifecycle = InvocationLifecycle(
        tracer,
        context_factory=DefaultInvocationContextFactory(),
    )
    invoker = CapabilityInvoker(
        registry,
        policy_engine,
        tracer,
        lifecycle=lifecycle,
    )
    validator = PlanValidator(RegistryCapabilityCatalog(registry))
    scheduler = BasicScheduler(invoker, tracer, lifecycle)
    return (
        ExecutionEngine(
            validator,
            scheduler,
            invoker,
            tracer,
            lifecycle,
            state_store=state_store,
        ),
        tracer,
    )


def make_request() -> Request:
    return Request(
        request_id="plan-request",
        input=RequestInput(type="json", content={"seed": 7}),
    )


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = ResultEnvelope.success(
            ResultOutput(
                type="json",
                data={
                    "score": 5,
                    "name": "alpha",
                    "a/b": {"~key": True},
                },
            )
        )
        self.results = {"n1": self.result}

    def condition(
        self,
        operator: ConditionOperator,
        value: object = None,
        *,
        pointer: str = "/output/data/score",
    ) -> ConditionExpr:
        return ConditionExpr(
            operator=operator,
            ref=ValueReference(node_id="n1", pointer=pointer),
            value=value,
        )

    def test_json_pointer_decodes_escaped_tokens(self) -> None:
        value = resolve_json_pointer(
            self.result.model_dump(mode="json"),
            "/output/data/a~1b/~0key",
        )

        self.assertIs(value, True)

    def test_comparison_and_exists_operators(self) -> None:
        evaluator = ConditionEvaluator()

        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.EQ, 5), self.results))
        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.NE, 4), self.results))
        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.LT, 6), self.results))
        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.LTE, 5), self.results))
        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.GT, 4), self.results))
        self.assertTrue(evaluator.evaluate(self.condition(ConditionOperator.GTE, 5), self.results))
        self.assertTrue(
            evaluator.evaluate(self.condition(ConditionOperator.IN, [4, 5]), self.results)
        )
        self.assertTrue(
            evaluator.evaluate(
                self.condition(ConditionOperator.EXISTS),
                self.results,
            )
        )
        self.assertFalse(
            evaluator.evaluate(
                self.condition(
                    ConditionOperator.EXISTS,
                    pointer="/output/data/missing",
                ),
                self.results,
            )
        )

    def test_logical_operators_compose_recursively(self) -> None:
        evaluator = ConditionEvaluator()
        equal = self.condition(ConditionOperator.EQ, 5)
        unequal = self.condition(ConditionOperator.EQ, 4)
        expression = ConditionExpr(
            operator=ConditionOperator.AND,
            operands=(
                equal,
                ConditionExpr(
                    operator=ConditionOperator.OR,
                    operands=(
                        unequal,
                        ConditionExpr(
                            operator=ConditionOperator.NOT,
                            operands=(unequal,),
                        ),
                    ),
                ),
            ),
        )

        self.assertTrue(evaluator.evaluate(expression, self.results))


class ExecutionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_checkpoints_running_and_terminal_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()
            tool = ReliabilityTool(
                "checkpoint.work/v1",
                delay=0.05,
                started=started,
            )
            database = Path(directory) / "execution.db"
            store = SQLiteStateStore(database)
            engine, _ = make_engine(tool, state_store=store)
            request = make_request()
            plan = ExecutionPlan(
                plan_id="checkpoint-plan",
                nodes=(PlanNode(node_id="work", capability="checkpoint.work/v1"),),
            )

            execution = asyncio.create_task(engine.execute(request, plan))
            await asyncio.wait_for(started.wait(), timeout=1)
            running = await SQLiteStateStore(database).load(plan.plan_id)

            self.assertEqual(running.state.status.value, "running")
            self.assertEqual(running.state.nodes["work"].status.value, "running")
            self.assertEqual(running.context.request, request)

            result = await execution
            terminal = await SQLiteStateStore(database).load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(terminal.plan, plan)
            self.assertEqual(terminal.state.status.value, "succeeded")
            self.assertEqual(terminal.state.nodes["work"].result.status, ResultStatus.SUCCESS)
            self.assertGreater(terminal.state_version, running.state_version)

    async def test_serial_nodes_resolve_request_and_node_output_bindings(self) -> None:
        calls: list[str] = []
        first = ControlledTool("step.first/v1", calls)
        second = ControlledTool("step.second/v1", calls)
        engine, tracer = make_engine(first, second)
        plan = ExecutionPlan(
            plan_id="serial",
            nodes=(
                PlanNode(
                    node_id="n1",
                    capability="step.first/v1",
                    input_mapping={
                        "label": LiteralBinding(value="first"),
                        "value": RequestBinding(pointer="/input/content/seed"),
                    },
                ),
                PlanNode(
                    node_id="n2",
                    capability="step.second/v1",
                    input_mapping={
                        "label": LiteralBinding(value="second"),
                        "value": NodeOutputBinding(
                            node_id="n1",
                            pointer="/output/data/value",
                        ),
                    },
                ),
            ),
            edges=(PlanEdge(from_node="n1", to_node="n2"),),
            outputs={
                "value": NodeOutputBinding(
                    node_id="n2",
                    pointer="/output/data/value",
                )
            },
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["value"], 7)
        self.assertEqual(calls, ["first", "second"])
        state = engine.state("serial")
        self.assertEqual(state.nodes["n1"].status, NodeExecutionStatus.SUCCEEDED)
        self.assertEqual(state.nodes["n2"].status, NodeExecutionStatus.SUCCEEDED)
        span_types = [span.type for span in tracer.spans(trace_id=result.trace_id)]
        self.assertIn(SpanType.PLAN, span_types)
        self.assertEqual(span_types.count(SpanType.PLAN_NODE), 2)

    async def test_parallel_roots_respect_plan_max_concurrency(self) -> None:
        calls: list[str] = []
        tracker = ConcurrencyTracker()
        tool = ControlledTool(
            "parallel.work/v1",
            calls,
            delay=0.02,
            tracker=tracker,
        )
        engine, _ = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="parallel",
            budget=PlanBudget(max_concurrency=2),
            nodes=tuple(
                PlanNode(
                    node_id=f"n{index}",
                    capability="parallel.work/v1",
                    input_mapping={"label": LiteralBinding(value=f"n{index}")},
                )
                for index in range(4)
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tracker.maximum, 2)
        self.assertCountEqual(calls, ["n0", "n1", "n2", "n3"])

    async def test_condition_branch_skips_inactive_node_and_join_still_runs(self) -> None:
        calls: list[str] = []
        tool = ControlledTool("branch.work/v1", calls)
        engine, _ = make_engine(tool)
        score_ref = ValueReference(node_id="source", pointer="/output/data/score")
        plan = ExecutionPlan(
            plan_id="branch",
            nodes=(
                PlanNode(
                    node_id="source",
                    capability="branch.work/v1",
                    input_mapping={
                        "label": LiteralBinding(value="source"),
                        "score": LiteralBinding(value=0.9),
                    },
                ),
                PlanNode(
                    node_id="high",
                    capability="branch.work/v1",
                    input_mapping={"label": LiteralBinding(value="high")},
                ),
                PlanNode(
                    node_id="low",
                    capability="branch.work/v1",
                    input_mapping={"label": LiteralBinding(value="low")},
                ),
                PlanNode(
                    node_id="join",
                    capability="branch.work/v1",
                    input_mapping={
                        "label": LiteralBinding(value="join"),
                        "selected": NodeOutputBinding(
                            node_id="high",
                            pointer="/output/data/label",
                        ),
                    },
                ),
            ),
            edges=(
                PlanEdge(
                    from_node="source",
                    to_node="high",
                    condition=ConditionExpr(
                        operator=ConditionOperator.GTE,
                        ref=score_ref,
                        value=0.8,
                    ),
                ),
                PlanEdge(
                    from_node="source",
                    to_node="low",
                    condition=ConditionExpr(
                        operator=ConditionOperator.LT,
                        ref=score_ref,
                        value=0.8,
                    ),
                ),
                PlanEdge(from_node="high", to_node="join"),
                PlanEdge(from_node="low", to_node="join"),
            ),
            outputs={
                "selected": NodeOutputBinding(
                    node_id="join",
                    pointer="/output/data/selected",
                )
            },
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["selected"], "high")
        self.assertEqual(calls, ["source", "high", "join"])
        state = engine.state("branch")
        self.assertEqual(state.nodes["low"].status, NodeExecutionStatus.SKIPPED)
        self.assertEqual(state.nodes["join"].status, NodeExecutionStatus.SUCCEEDED)

    async def test_continue_failure_produces_partial_when_outputs_are_available(self) -> None:
        calls: list[str] = []
        failing = ControlledTool("work.fail/v1", calls, fail=True)
        successful = ControlledTool("work.ok/v1", calls)
        engine, _ = make_engine(failing, successful)
        plan = ExecutionPlan(
            plan_id="partial",
            nodes=(
                PlanNode(
                    node_id="bad",
                    capability="work.fail/v1",
                    failure_policy=FailurePolicy.CONTINUE,
                ),
                PlanNode(
                    node_id="good",
                    capability="work.ok/v1",
                    input_mapping={"value": LiteralBinding(value=42)},
                ),
            ),
            outputs={
                "value": NodeOutputBinding(
                    node_id="good",
                    pointer="/output/data/value",
                )
            },
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.output.data["value"], 42)
        self.assertEqual(result.issues[0].source, "bad")

    async def test_fail_plan_stops_unstarted_nodes(self) -> None:
        calls: list[str] = []
        failing = ControlledTool("work.fail/v1", calls, fail=True)
        successful = ControlledTool("work.ok/v1", calls)
        engine, _ = make_engine(failing, successful)
        plan = ExecutionPlan(
            plan_id="fail-fast",
            budget=PlanBudget(max_concurrency=1),
            nodes=(
                PlanNode(node_id="bad", capability="work.fail/v1"),
                PlanNode(node_id="never", capability="work.ok/v1"),
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(calls, ["work.fail/v1"])
        state = engine.state("fail-fast")
        self.assertEqual(state.nodes["bad"].status, NodeExecutionStatus.FAILED)
        self.assertEqual(state.nodes["never"].status, NodeExecutionStatus.CANCELLED)

    async def test_retryable_read_failure_retries_and_records_attempt_count(self) -> None:
        tool = ReliabilityTool(
            "retry.read/v1",
            failures_before_success=1,
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        engine, tracer = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="retry-read",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="retry.read/v1",
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 2)
        self.assertEqual(engine.state("retry-read").nodes["work"].attempt, 2)
        events = tracer.events(trace_id=result.trace_id)
        self.assertEqual([event.name for event in events].count("node.retrying"), 2)

    async def test_retry_exhaustion_preserves_last_failure(self) -> None:
        tool = ReliabilityTool(
            "retry.exhausted/v1",
            failures_before_success=10,
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        engine, _ = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="retry-exhausted",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="retry.exhausted/v1",
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.message, "transient failure")
        self.assertEqual(tool.calls, 3)
        self.assertEqual(engine.state("retry-exhausted").nodes["work"].attempt, 3)

    async def test_retry_exhaustion_falls_back_without_scheduler_reselection(self) -> None:
        capability_id = "retry.fallback/v1"
        primary = ReliabilityTool(
            capability_id,
            failures_before_success=10,
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
            fallbackable=True,
        )
        backup = ReliabilityTool(
            capability_id,
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        registry = InMemoryCapabilityRegistry()
        registry.register_provider(
            primary,
            descriptor=ProviderDescriptor(
                provider_id="retry-primary",
                capability_id=capability_id,
                plugin_id="primary-plugin",
                implementation_version="1.0.0",
                priority=100,
            ),
        )
        registry.register_provider(
            backup,
            descriptor=ProviderDescriptor(
                provider_id="retry-backup",
                capability_id=capability_id,
                plugin_id="backup-plugin",
                implementation_version="1.0.0",
                priority=10,
            ),
        )
        engine, _ = make_engine_from_registry(registry)
        plan = ExecutionPlan(
            plan_id="retry-fallback",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability=capability_id,
                    retry_policy=RetryPolicy(
                        max_attempts=2,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 1)
        self.assertEqual(engine.state(plan.plan_id).nodes["work"].attempt, 3)

    async def test_write_retry_requires_supported_idempotency_and_key(self) -> None:
        unsafe = ReliabilityTool(
            "retry.unsafe-write/v1",
            failures_before_success=1,
            profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.NONE,
            ),
        )
        safe = ReliabilityTool(
            "retry.safe-write/v1",
            failures_before_success=1,
            profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.REQUIRED,
            ),
        )
        retry = RetryPolicy(max_attempts=2, initial_backoff_ms=0, max_backoff_ms=0)
        unsafe_engine, _ = make_engine(unsafe)
        safe_engine, _ = make_engine(safe)

        unsafe_result = await unsafe_engine.execute(
            make_request(),
            ExecutionPlan(
                plan_id="unsafe-write",
                nodes=(
                    PlanNode(
                        node_id="work",
                        capability="retry.unsafe-write/v1",
                        retry_policy=retry,
                    ),
                ),
            ),
        )
        safe_result = await safe_engine.execute(
            make_request(),
            ExecutionPlan(
                plan_id="safe-write",
                nodes=(
                    PlanNode(
                        node_id="work",
                        capability="retry.safe-write/v1",
                        retry_policy=retry,
                        idempotency_key="payment-42",
                    ),
                ),
            ),
        )

        self.assertEqual(unsafe_result.status, ResultStatus.FAILED)
        self.assertEqual(unsafe.calls, 1)
        self.assertEqual(safe_result.status, ResultStatus.SUCCESS)
        self.assertEqual(safe.calls, 2)
        self.assertEqual(safe.contexts[0].attributes["idempotency_key"], "payment-42")

    async def test_node_timeout_is_one_absolute_deadline_across_retries(self) -> None:
        tool = ReliabilityTool(
            "retry.timeout/v1",
            delay=0.1,
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        engine, _ = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="deadline",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="retry.timeout/v1",
                    timeout_ms=20,
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.TIMEOUT")
        self.assertEqual(tool.calls, 1)
        self.assertIsNotNone(tool.contexts[0].deadline_at)

    async def test_explicit_cancel_stops_running_and_unstarted_nodes(self) -> None:
        started = asyncio.Event()
        tool = ReliabilityTool("cancel.work/v1", delay=10, started=started)
        engine, _ = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="cancel-plan",
            budget=PlanBudget(max_concurrency=1),
            nodes=(
                PlanNode(node_id="running", capability="cancel.work/v1"),
                PlanNode(node_id="pending", capability="cancel.work/v1"),
            ),
        )
        execution = asyncio.create_task(engine.execute(make_request(), plan))
        await asyncio.wait_for(started.wait(), timeout=1)

        cancelled = await engine.cancel("cancel-plan", "user requested")
        result = await asyncio.wait_for(execution, timeout=1)

        self.assertIs(cancelled, True)
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertEqual(result.metadata["reason"], "user requested")
        self.assertTrue(tool.cancelled)
        state = engine.state("cancel-plan")
        self.assertEqual(state.nodes["running"].status, NodeExecutionStatus.CANCELLED)
        self.assertEqual(state.nodes["pending"].status, NodeExecutionStatus.CANCELLED)
        record = await engine.state_store.load("cancel-plan")
        self.assertTrue(record.context.cancellation.cancelled)
        self.assertEqual(record.context.cancellation.reason, "user requested")
        self.assertIs(await engine.cancel("unknown-plan"), False)

    async def test_cancel_before_scheduler_run_prevents_provider_call(self) -> None:
        tool = ReliabilityTool("cancel.before-run/v1")
        engine, _ = make_engine(tool)
        request = make_request()
        plan = ExecutionPlan(
            plan_id="cancel-before-run",
            nodes=(PlanNode(node_id="work", capability="cancel.before-run/v1"),),
        )
        signal = CancellationSignal()
        self.assertTrue(signal.request("cancelled before scheduling"))

        outcome = await engine.scheduler.run(
            request,
            plan,
            InvocationContext(request=request),
            parent=None,
            trace_enabled=False,
            cancellation=signal,
        )

        self.assertEqual(outcome.result.status, ResultStatus.CANCELLED)
        self.assertEqual(outcome.state.nodes["work"].status, NodeExecutionStatus.CANCELLED)
        self.assertEqual(tool.calls, 0)
        self.assertFalse(signal.request("duplicate request"))

    async def test_client_task_cancellation_still_propagates(self) -> None:
        started = asyncio.Event()
        tool = ReliabilityTool("cancel.client/v1", delay=10, started=started)
        engine, _ = make_engine(tool)
        execution = asyncio.create_task(
            engine.execute(
                make_request(),
                ExecutionPlan(
                    plan_id="client-cancel",
                    nodes=(PlanNode(node_id="work", capability="cancel.client/v1"),),
                ),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        execution.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertTrue(tool.cancelled)
        self.assertIs(await engine.cancel("client-cancel"), False)


if __name__ == "__main__":
    unittest.main()
