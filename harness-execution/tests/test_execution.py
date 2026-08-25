"""Basic Scheduler 与 ExecutionEngine 行为测试。"""

from __future__ import annotations

import asyncio
import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityType,
    ConditionExpr,
    ConditionOperator,
    ExecutionPlan,
    FailurePolicy,
    InvocationContext,
    LiteralBinding,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanBudget,
    PlanEdge,
    PlanNode,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    ValueReference,
)
from harness_execution import (
    BasicScheduler,
    ConditionEvaluator,
    ExecutionEngine,
    resolve_json_pointer,
)
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
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
                return ResultEnvelope.failure(
                    CapabilityError("controlled failure").to_detail()
                )
            return ResultEnvelope.success(ResultOutput(type="json", data=arguments))
        finally:
            if self._tracker is not None:
                self._tracker.current -= 1


def make_engine(*providers: ToolSPI) -> tuple[ExecutionEngine, InMemoryTracer]:
    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="execution-tests")
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
    return ExecutionEngine(validator, scheduler, invoker, tracer, lifecycle), tracer


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


if __name__ == "__main__":
    unittest.main()
