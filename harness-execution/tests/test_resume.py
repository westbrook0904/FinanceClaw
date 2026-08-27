"""ExecutionEngine checkpoint resume 行为测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    Continuation,
    ExecutionPlan,
    IdempotencyType,
    IdentityContext,
    InvocationContext,
    NodeExecutionState,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanEdge,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
    TenantContext,
    TraceContext,
)
from harness_execution import BasicScheduler, ExecutionEngine
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, SQLiteStateStore, StateStore
from harness_trace import InMemoryTracer


class ResumeTool(ToolSPI):
    def __init__(
        self,
        capability_id: str,
        *,
        profile: CapabilityExecutionProfile | None = None,
        delay: float = 0,
        started: asyncio.Event | None = None,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self.delay = delay
        self.started = started
        self.calls = 0
        self.arguments: list[dict[str, object]] = []
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
        arguments = request.model_dump(mode="json")["arguments"]
        self.arguments.append(arguments)
        if self.started is not None:
            self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        return ResultEnvelope.success(
            ResultOutput(type="json", data={**arguments, "calls": self.calls})
        )


def make_engine(
    *providers: ToolSPI,
    state_store: StateStore,
) -> ExecutionEngine:
    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="resume-tests")
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
        request_id="resume-request",
        input=RequestInput(type="json", content={"seed": 7}),
    )


class FailingLoadStateStore(StateStore):
    async def create(self, record: PlanExecutionRecord) -> None:
        raise AssertionError("create should not be called")

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        raise RuntimeError("corrupt state payload")

    async def save(self, record: PlanExecutionRecord) -> None:
        raise AssertionError("save should not be called")

    async def delete(self, plan_id: str) -> None:
        return None


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_running_checkpoint_can_resume_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()
            first_tool = ResumeTool(
                "resume.work/v1",
                delay=10,
                started=started,
            )
            live_database = Path(directory) / "live.db"
            live_engine = make_engine(
                first_tool,
                state_store=SQLiteStateStore(live_database),
            )
            plan = ExecutionPlan(
                plan_id="restart-plan",
                nodes=(PlanNode(node_id="work", capability="resume.work/v1"),),
            )
            execution = asyncio.create_task(live_engine.execute(make_request(), plan))
            await asyncio.wait_for(started.wait(), timeout=1)

            running = await SQLiteStateStore(live_database).load(plan.plan_id)
            self.assertIsNotNone(running)
            self.assertEqual(running.state.nodes["work"].status, NodeExecutionStatus.RUNNING)

            # 复制进程崩溃时已经落盘的原子快照。随后取消旧 Task 仅用于测试清理，
            # 不会修改 restart.db，因此新 Engine 看到的仍是 RUNNING checkpoint。
            restart_database = Path(directory) / "restart.db"
            restart_store = SQLiteStateStore(restart_database)
            await restart_store.create(running)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution

            resumed_tool = ResumeTool("resume.work/v1")
            resumed_engine = make_engine(resumed_tool, state_store=restart_store)
            result = await resumed_engine.resume(plan.plan_id)
            saved = await SQLiteStateStore(restart_database).load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(resumed_tool.calls, 1)
            self.assertEqual(saved.state.status, PlanExecutionStatus.SUCCEEDED)
            self.assertEqual(saved.state.nodes["work"].status, NodeExecutionStatus.SUCCEEDED)
            self.assertGreater(saved.state_version, running.state_version)


    async def test_resume_reuses_completed_results_without_rerunning_completed_nodes(self) -> None:
        tool = ResumeTool("resume.binding/v1")
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="resume-bindings",
            nodes=(
                PlanNode(node_id="done", capability="resume.binding/v1"),
                PlanNode(
                    node_id="work",
                    capability="resume.binding/v1",
                    input_mapping={
                        "value": NodeOutputBinding(
                            node_id="done",
                            pointer="/output/data/value",
                        )
                    },
                ),
            ),
            edges=(PlanEdge(from_node="done", to_node="work"),),
            outputs={
                "value": NodeOutputBinding(
                    node_id="work",
                    pointer="/output/data/value",
                )
            },
        )
        completed = ResultEnvelope.success(
            ResultOutput(type="json", data={"value": 7})
        )
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(request=make_request()),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "done": NodeExecutionState(
                            node_id="done",
                            status=NodeExecutionStatus.SUCCEEDED,
                            attempt=1,
                            started_at=datetime.now(UTC) - timedelta(seconds=2),
                            completed_at=datetime.now(UTC) - timedelta(seconds=1),
                            result=completed,
                        ),
                        "work": NodeExecutionState(
                            node_id="work",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=1,
                            started_at=datetime.now(UTC),
                        ),
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(tool.arguments, [{"value": 7}])
        self.assertEqual(result.output.data["value"], 7)

    async def test_resume_preserves_persisted_trusted_context(self) -> None:
        tool = ResumeTool("resume.context/v1")
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="resume-context",
            nodes=(PlanNode(node_id="work", capability="resume.context/v1"),),
        )
        deadline_at = datetime.now(UTC) + timedelta(minutes=5)
        persisted_context = InvocationContext(
            request=make_request(),
            identity=IdentityContext(
                subject="user-42",
                scopes=frozenset({"portfolio:read"}),
            ),
            tenant=TenantContext(tenant_id="tenant-a"),
            deadline_at=deadline_at,
            attributes={"trusted_source": "gateway"},
            trace_context=TraceContext(trace_id="resume-trace", span_id="old-plan-span"),
        )
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=persisted_context,
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "work": NodeExecutionState(
                            node_id="work",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=1,
                            started_at=datetime.now(UTC),
                        )
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 1)
        resumed_context = tool.contexts[0]
        self.assertEqual(resumed_context.identity, persisted_context.identity)
        self.assertEqual(resumed_context.tenant, persisted_context.tenant)
        self.assertEqual(resumed_context.deadline_at, deadline_at)
        self.assertEqual(resumed_context.attributes["trusted_source"], "gateway")
        self.assertEqual(resumed_context.trace_context.trace_id, "resume-trace")

    async def test_resume_refuses_non_idempotent_interrupted_write(self) -> None:
        tool = ResumeTool(
            "resume.write/v1",
            profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.NONE,
            ),
        )
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="unsafe-resume",
            nodes=(PlanNode(node_id="write", capability="resume.write/v1"),),
        )
        started_at = datetime.now(UTC)
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(request=make_request()),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "write": NodeExecutionState(
                            node_id="write",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=1,
                            started_at=started_at,
                        )
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.RESUME_UNSAFE")
        self.assertEqual(tool.calls, 0)
        self.assertEqual(saved.state.nodes["write"].status, NodeExecutionStatus.RUNNING)

    async def test_resume_allows_idempotent_interrupted_write_with_key(self) -> None:
        tool = ResumeTool(
            "resume.safe-write/v1",
            profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.REQUIRED,
            ),
        )
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="safe-resume",
            nodes=(
                PlanNode(
                    node_id="write",
                    capability="resume.safe-write/v1",
                    idempotency_key="transfer-42",
                    retry_policy=RetryPolicy(max_attempts=3),
                ),
            ),
        )
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(request=make_request()),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "write": NodeExecutionState(
                            node_id="write",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=2,
                            started_at=datetime.now(UTC),
                        )
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(engine.state(plan.plan_id).nodes["write"].attempt, 2)

    async def test_waiting_resume_is_idempotent_until_external_resolution(self) -> None:
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="waiting-resume",
            nodes=(
                PlanNode(node_id="approval", kind=PlanNodeKind.APPROVAL),
                PlanNode(node_id="after", capability="resume.after-approval/v1"),
            ),
            edges=(PlanEdge(from_node="approval", to_node="after"),),
        )
        continuation = Continuation(
            plan_id=plan.plan_id,
            node_id="approval",
            waiting_reason="approval",
        )
        accepted = ResultEnvelope.accepted(continuation)
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
                        "approval": NodeExecutionState(
                            node_id="approval",
                            status=NodeExecutionStatus.WAITING,
                            attempt=1,
                            started_at=datetime.now(UTC),
                            result=accepted,
                            waiting_reason="approval",
                            continuation=continuation,
                        ),
                        "after": NodeExecutionState(node_id="after"),
                    },
                ),
            )
        )
        after = ResumeTool("resume.after-approval/v1")
        engine = make_engine(after, state_store=store)

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.ACCEPTED)
        self.assertEqual(result.continuation, saved.state.nodes["approval"].continuation)
        self.assertEqual(after.calls, 0)
        self.assertEqual(saved.state.status, PlanExecutionStatus.WAITING)

        repeated = await engine.resume(plan.plan_id)
        self.assertEqual(after.calls, 0)
        self.assertEqual(repeated.status, ResultStatus.ACCEPTED)

    async def test_missing_resume_state_returns_structured_failure(self) -> None:
        engine = make_engine(state_store=InMemoryStateStore())

        result = await engine.resume("missing-plan")

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.NOT_FOUND")

    async def test_state_load_failure_returns_structured_failure(self) -> None:
        engine = make_engine(state_store=FailingLoadStateStore())

        result = await engine.resume("corrupt-plan")

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.STATE_LOAD_FAILED")
        self.assertEqual(result.error.details["cause_type"], "RuntimeError")

    async def test_resume_does_not_reset_interrupted_node_timeout(self) -> None:
        tool = ResumeTool("resume.node-timeout/v1")
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="expired-node-timeout",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="resume.node-timeout/v1",
                    timeout_ms=20,
                ),
            ),
        )
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(request=make_request()),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "work": NodeExecutionState(
                            node_id="work",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=1,
                            started_at=datetime.now(UTC) - timedelta(seconds=1),
                        )
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.TIMEOUT")
        self.assertEqual(tool.calls, 0)
        self.assertEqual(saved.state.nodes["work"].status, NodeExecutionStatus.FAILED)

    async def test_resume_does_not_reset_expired_original_deadline(self) -> None:
        tool = ResumeTool("resume.deadline/v1")
        store = InMemoryStateStore()
        plan = ExecutionPlan(
            plan_id="expired-resume",
            nodes=(PlanNode(node_id="work", capability="resume.deadline/v1"),),
        )
        await store.create(
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=InvocationContext(
                    request=make_request(),
                    deadline_at=datetime.now(UTC) - timedelta(seconds=1),
                ),
                state=PlanExecutionState(
                    plan_id=plan.plan_id,
                    plan_revision=plan.revision,
                    status=PlanExecutionStatus.RUNNING,
                    nodes={
                        "work": NodeExecutionState(
                            node_id="work",
                            status=NodeExecutionStatus.RUNNING,
                            attempt=1,
                            started_at=datetime.now(UTC) - timedelta(seconds=2),
                        )
                    },
                ),
            )
        )
        engine = make_engine(tool, state_store=store)

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.TIMEOUT")
        self.assertEqual(tool.calls, 0)
        self.assertEqual(saved.state.status, PlanExecutionStatus.FAILED)

        repeated = await engine.resume(plan.plan_id)
        repeated_saved = await store.load(plan.plan_id)
        self.assertEqual(repeated.status, ResultStatus.FAILED)
        self.assertEqual(repeated.error.code, "HARNESS.TIMEOUT")
        self.assertEqual(tool.calls, 0)
        self.assertEqual(repeated_saved.state_version, saved.state_version)


if __name__ == "__main__":
    unittest.main()
