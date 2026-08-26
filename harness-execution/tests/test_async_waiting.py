"""Async WAITING / completion / resume 行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    Continuation,
    EdgeTrigger,
    ExecutionPlan,
    FailurePolicy,
    InvocationContext,
    LiteralBinding,
    NodeExecutionState,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanBudget,
    PlanEdge,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    Request,
    RequestError,
    RequestInput,
    ResultEnvelope,
    ResultIssue,
    ResultOutput,
    ResultStatus,
)
from harness_execution import BasicScheduler, ExecutionEngine
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, SQLiteStateStore, StateStore
from harness_trace import InMemoryTracer


class AsyncStartTool(ToolSPI):
    def __init__(
        self,
        capability_id: str = "async.start/v1",
        *,
        job_ref: str = "job-1",
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.job_ref = job_ref
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope.accepted(
            Continuation(job_ref=self.job_ref, waiting_reason="external_job")
        )


class MissingJobRefTool(ToolSPI):
    def __init__(self) -> None:
        self._descriptor = CapabilityDescriptor(
            id="async.invalid/v1",
            name="async.invalid/v1",
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
        return ResultEnvelope.accepted(
            Continuation(node_id="async", waiting_reason="external_job")
        )


class EchoTool(ToolSPI):
    def __init__(self, capability_id: str = "async.echo/v1") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.calls: list[dict[str, object]] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        arguments = request.model_dump(mode="json")["arguments"]
        self.calls.append(arguments)
        return ResultEnvelope.success(ResultOutput(type="json", data=arguments))


def make_engine(
    *providers: ToolSPI,
    state_store: StateStore,
) -> ExecutionEngine:
    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="async-tests")
    tracer = InMemoryTracer()
    lifecycle = InvocationLifecycle(
        tracer,
        context_factory=DefaultInvocationContextFactory(),
    )
    invoker = CapabilityInvoker(
        registry,
        PolicyEngine((AllowAllPolicy(),)),
        tracer,
        lifecycle=lifecycle,
    )
    validator = PlanValidator(RegistryCapabilityCatalog(registry))
    scheduler = BasicScheduler(invoker, tracer, lifecycle)
    return ExecutionEngine(
        validator,
        scheduler,
        invoker,
        tracer,
        lifecycle,
        state_store=state_store,
    )


def make_request() -> Request:
    return Request(
        request_id="async-request",
        input=RequestInput(type="json", content={"input": "value"}),
    )


class AsyncWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_accepted_persists_job_and_completion_resumes_downstream(self) -> None:
        starter = AsyncStartTool(job_ref="provider-job-7")
        echo = EchoTool()
        store = InMemoryStateStore()
        engine = make_engine(starter, echo, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-success",
            nodes=(
                PlanNode(node_id="async", capability="async.start/v1"),
                PlanNode(
                    node_id="after",
                    capability="async.echo/v1",
                    input_mapping={
                        "value": NodeOutputBinding(
                            node_id="async",
                            pointer="/output/data/value",
                        )
                    },
                ),
            ),
            edges=(PlanEdge(from_node="async", to_node="after"),),
            outputs={
                "value": NodeOutputBinding(
                    node_id="after",
                    pointer="/output/data/value",
                )
            },
        )

        waiting = await engine.execute(make_request(), plan)
        saved_waiting = await store.load(plan.plan_id)

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(waiting.continuation.plan_id, plan.plan_id)
        self.assertEqual(waiting.continuation.node_id, "async")
        self.assertEqual(waiting.continuation.job_ref, "provider-job-7")
        self.assertEqual(len(saved_waiting.state.pending_jobs), 1)
        self.assertEqual(saved_waiting.state.pending_jobs[0], waiting.continuation)
        self.assertEqual(starter.calls, 1)
        self.assertEqual(echo.calls, [])

        result = await engine.complete_async_node(
            plan.plan_id,
            "async",
            ResultEnvelope.success(ResultOutput(type="json", data={"value": 7})),
        )
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["value"], 7)
        self.assertEqual(starter.calls, 1)
        self.assertEqual(echo.calls, [{"value": 7}])
        self.assertEqual(saved.state.pending_jobs, [])
        self.assertEqual(saved.state.nodes["async"].status, NodeExecutionStatus.SUCCEEDED)
        self.assertEqual(saved.state.nodes["after"].status, NodeExecutionStatus.SUCCEEDED)
        self.assertEqual(saved.state.metadata["async_completions"][0]["job_ref"], "provider-job-7")

    async def test_sqlite_async_completion_works_after_engine_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "async.db"
            first_starter = AsyncStartTool(job_ref="restart-job")
            first_echo = EchoTool()
            first_engine = make_engine(
                first_starter,
                first_echo,
                state_store=SQLiteStateStore(database),
            )
            plan = ExecutionPlan(
                plan_id="async-restart",
                nodes=(
                    PlanNode(node_id="async", capability="async.start/v1"),
                    PlanNode(
                        node_id="after",
                        capability="async.echo/v1",
                        input_mapping={
                            "value": NodeOutputBinding(
                                node_id="async",
                                pointer="/output/data/value",
                            )
                        },
                    ),
                ),
                edges=(PlanEdge(from_node="async", to_node="after"),),
                outputs={
                    "value": NodeOutputBinding(
                        node_id="after",
                        pointer="/output/data/value",
                    )
                },
            )
            waiting = await first_engine.execute(make_request(), plan)
            self.assertEqual(waiting.status, ResultStatus.ACCEPTED)

            resumed_starter = AsyncStartTool(job_ref="restart-job")
            resumed_echo = EchoTool()
            resumed_engine = make_engine(
                resumed_starter,
                resumed_echo,
                state_store=SQLiteStateStore(database),
            )
            result = await resumed_engine.complete_async_node(
                plan.plan_id,
                "async",
                ResultEnvelope.success(
                    ResultOutput(type="json", data={"value": "after-restart"})
                ),
            )
            saved = await SQLiteStateStore(database).load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.output.data["value"], "after-restart")
            self.assertEqual(first_starter.calls, 1)
            self.assertEqual(resumed_starter.calls, 0)
            self.assertEqual(resumed_echo.calls, [{"value": "after-restart"}])
            self.assertEqual(saved.state.pending_jobs, [])

    async def test_failed_async_completion_can_follow_failed_edge(self) -> None:
        starter = AsyncStartTool()
        success_branch = EchoTool("async.success/v1")
        failure_branch = EchoTool("async.failure/v1")
        store = InMemoryStateStore()
        engine = make_engine(
            starter,
            success_branch,
            failure_branch,
            state_store=store,
        )
        plan = ExecutionPlan(
            plan_id="async-failed-edge",
            nodes=(
                PlanNode(
                    node_id="async",
                    capability="async.start/v1",
                    failure_policy=FailurePolicy.CONTINUE,
                ),
                PlanNode(
                    node_id="success",
                    capability="async.success/v1",
                    input_mapping={"branch": LiteralBinding(value="success")},
                ),
                PlanNode(
                    node_id="failure",
                    capability="async.failure/v1",
                    input_mapping={"branch": LiteralBinding(value="failure")},
                ),
            ),
            edges=(
                PlanEdge(from_node="async", to_node="success", trigger=EdgeTrigger.SUCCESS),
                PlanEdge(from_node="async", to_node="failure", trigger=EdgeTrigger.FAILED),
            ),
            outputs={
                "branch": NodeOutputBinding(
                    node_id="failure",
                    pointer="/output/data/branch",
                )
            },
        )
        await engine.execute(make_request(), plan)

        result = await engine.complete_async_node(
            plan.plan_id,
            "async",
            ResultEnvelope.failure(
                RequestError("external job failed", code="ASYNC.TEST.FAIL").to_detail()
            ),
        )
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.output.data["branch"], "failure")
        self.assertEqual(success_branch.calls, [])
        self.assertEqual(failure_branch.calls, [{"branch": "failure"}])
        self.assertEqual(saved.state.nodes["async"].status, NodeExecutionStatus.FAILED)
        self.assertEqual(saved.state.nodes["success"].status, NodeExecutionStatus.SKIPPED)

    async def test_partial_terminal_result_remains_available_to_downstream_binding(self) -> None:
        starter = AsyncStartTool()
        echo = EchoTool()
        store = InMemoryStateStore()
        engine = make_engine(starter, echo, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-partial",
            nodes=(
                PlanNode(node_id="async", capability="async.start/v1"),
                PlanNode(
                    node_id="after",
                    capability="async.echo/v1",
                    input_mapping={
                        "value": NodeOutputBinding(
                            node_id="async",
                            pointer="/output/data/value",
                        )
                    },
                ),
            ),
            edges=(PlanEdge(from_node="async", to_node="after"),),
            outputs={
                "value": NodeOutputBinding(
                    node_id="after",
                    pointer="/output/data/value",
                )
            },
        )
        await engine.execute(make_request(), plan)
        issue_error = RequestError("one field unavailable", code="ASYNC.TEST.PARTIAL")

        result = await engine.complete_async_node(
            plan.plan_id,
            "async",
            ResultEnvelope.partial(
                ResultOutput(type="json", data={"value": 9}),
                (ResultIssue(source="external-job", error=issue_error.to_detail()),),
            ),
        )

        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.output.data["value"], 9)
        self.assertEqual(echo.calls, [{"value": 9}])

    async def test_nonterminal_completion_and_duplicate_completion_are_rejected(self) -> None:
        starter = AsyncStartTool()
        store = InMemoryStateStore()
        engine = make_engine(starter, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-duplicate",
            nodes=(PlanNode(node_id="async", capability="async.start/v1"),),
        )
        waiting = await engine.execute(make_request(), plan)

        nonterminal = await engine.complete_async_node(
            plan.plan_id,
            "async",
            ResultEnvelope.accepted(
                Continuation(job_ref="another-job", waiting_reason="external_job")
            ),
        )
        self.assertEqual(nonterminal.status, ResultStatus.FAILED)
        self.assertEqual(nonterminal.error.code, "HARNESS.ASYNC.RESULT_NOT_TERMINAL")

        missing = await engine.complete_async_node(
            plan.plan_id,
            "unknown",
            ResultEnvelope.success(ResultOutput(type="json", data={})),
        )
        self.assertEqual(missing.status, ResultStatus.FAILED)
        self.assertEqual(missing.error.code, "HARNESS.ASYNC.NOT_PENDING")

        first = await engine.complete_async_node(
            plan.plan_id,
            waiting.continuation.node_id,
            ResultEnvelope.success(ResultOutput(type="json", data={})),
        )
        duplicate = await engine.complete_async_node(
            plan.plan_id,
            "async",
            ResultEnvelope.success(ResultOutput(type="json", data={})),
        )
        self.assertEqual(first.status, ResultStatus.SUCCESS)
        self.assertEqual(duplicate.status, ResultStatus.FAILED)
        self.assertEqual(duplicate.error.code, "HARNESS.ASYNC.NOT_PENDING")
        self.assertEqual(starter.calls, 1)

    async def test_parallel_async_jobs_are_independently_completable(self) -> None:
        first = AsyncStartTool("async.first/v1", job_ref="job-a")
        second = AsyncStartTool("async.second/v1", job_ref="job-b")
        store = InMemoryStateStore()
        engine = make_engine(first, second, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-parallel",
            budget=PlanBudget(max_concurrency=2),
            nodes=(
                PlanNode(node_id="a", capability="async.first/v1"),
                PlanNode(node_id="b", capability="async.second/v1"),
            ),
        )
        waiting = await engine.execute(make_request(), plan)
        initial = await store.load(plan.plan_id)

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(len(initial.state.pending_jobs), 2)

        still_waiting = await engine.complete_async_node(
            plan.plan_id,
            "a",
            ResultEnvelope.success(ResultOutput(type="json", data={"a": 1})),
        )
        after_first = await store.load(plan.plan_id)
        self.assertEqual(still_waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(still_waiting.continuation.node_id, "b")
        self.assertEqual(len(after_first.state.pending_jobs), 1)
        self.assertEqual(after_first.state.pending_jobs[0].node_id, "b")

        completed = await engine.complete_async_node(
            plan.plan_id,
            "b",
            ResultEnvelope.success(ResultOutput(type="json", data={"b": 2})),
        )
        self.assertEqual(completed.status, ResultStatus.SUCCESS)
        self.assertEqual(completed.output.data, {})
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    async def test_resume_repairs_waiting_checkpoint_before_job_materialization(self) -> None:
        starter = AsyncStartTool(job_ref="repair-job")
        store = InMemoryStateStore()
        engine = make_engine(starter, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-repair",
            nodes=(PlanNode(node_id="async", capability="async.start/v1"),),
        )
        continuation = Continuation(job_ref="repair-job", waiting_reason="external_job")
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(request=make_request()),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.WAITING,
                    nodes={
                        "async": NodeExecutionState(
                            node_id="async",
                            status=NodeExecutionStatus.WAITING,
                            attempt=1,
                            result=ResultEnvelope.accepted(continuation),
                            waiting_reason="external_job",
                            continuation=continuation,
                        )
                    },
                ),
            )
        )

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.ACCEPTED)
        self.assertEqual(result.continuation.plan_id, plan.plan_id)
        self.assertEqual(result.continuation.node_id, "async")
        self.assertEqual(result.continuation.job_ref, "repair-job")
        self.assertEqual(len(saved.state.pending_jobs), 1)
        self.assertEqual(saved.state.pending_jobs[0], result.continuation)
        self.assertEqual(starter.calls, 0)

    async def test_async_provider_must_return_job_ref(self) -> None:
        provider = MissingJobRefTool()
        store = InMemoryStateStore()
        engine = make_engine(provider, state_store=store)
        plan = ExecutionPlan(
            plan_id="async-job-ref-required",
            nodes=(PlanNode(node_id="async", capability="async.invalid/v1"),),
        )

        result = await engine.execute(make_request(), plan)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ASYNC.JOB_REF_REQUIRED")
        self.assertEqual(saved.state.nodes["async"].status, NodeExecutionStatus.WAITING)
        self.assertEqual(saved.state.pending_jobs, [])


if __name__ == "__main__":
    unittest.main()
