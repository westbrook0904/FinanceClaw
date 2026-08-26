"""显式 Human Approval WAITING / resolve / resume 行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
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
)
from harness_execution import BasicScheduler, ExecutionEngine
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, SQLiteStateStore, StateStore
from harness_trace import InMemoryTracer


class ApprovalTool(ToolSPI):
    def __init__(self, capability_id: str = "approval.work/v1") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.calls: list[dict[str, object]] = []
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        arguments = request.model_dump(mode="json")["arguments"]
        self.calls.append(arguments)
        self.contexts.append(context)
        return ResultEnvelope.success(ResultOutput(type="json", data=arguments))


def make_engine(
    *providers: ToolSPI,
    state_store: StateStore,
) -> ExecutionEngine:
    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="approval-tests")
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
        request_id="approval-request",
        input=RequestInput(type="json", content={"secret": "must-not-leak"}),
    )


def approval_node(
    *,
    failure_policy: FailurePolicy = FailurePolicy.FAIL_PLAN,
) -> PlanNode:
    return PlanNode(
        node_id="approve",
        kind=PlanNodeKind.APPROVAL,
        failure_policy=failure_policy,
        metadata={
            "approval_reason": "confirm external operation",
            "approval_resource_category": "external_action",
            "approval_parameter_names": ["account", "amount"],
            # 任意 metadata 不会被复制到 ApprovalRequest。
            "secret": "never-persist-in-approval-request",
        },
    )


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_approval_waits_and_persists_safe_request(self) -> None:
        tool = ApprovalTool()
        store = InMemoryStateStore()
        engine = make_engine(tool, state_store=store)
        plan = ExecutionPlan(
            plan_id="approval-wait",
            nodes=(
                approval_node(),
                PlanNode(
                    node_id="after",
                    capability="approval.work/v1",
                    input_mapping={"status": LiteralBinding(value="approved")},
                ),
            ),
            edges=(PlanEdge(from_node="approve", to_node="after"),),
        )

        result = await engine.execute(make_request(), plan)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.ACCEPTED)
        self.assertEqual(tool.calls, [])
        self.assertIsNotNone(result.continuation.approval_id)
        self.assertEqual(saved.state.nodes["approve"].status, NodeExecutionStatus.WAITING)
        self.assertEqual(len(saved.state.pending_approvals), 1)
        pending = saved.state.pending_approvals[0]
        self.assertEqual(pending.approval_id, result.continuation.approval_id)
        self.assertEqual(pending.reason, "confirm external operation")
        self.assertEqual(pending.resource_category, "external_action")
        self.assertEqual(
            pending.model_dump(mode="json")["parameter_summary"],
            {"parameter_names": ["account", "amount"]},
        )
        self.assertNotIn("secret", pending.model_dump_json())
        self.assertFalse(await engine.cancel(plan.plan_id))

    async def test_approved_decision_resumes_downstream_capability(self) -> None:
        tool = ApprovalTool()
        store = InMemoryStateStore()
        engine = make_engine(tool, state_store=store)
        plan = ExecutionPlan(
            plan_id="approval-approved",
            nodes=(
                approval_node(),
                PlanNode(
                    node_id="after",
                    capability="approval.work/v1",
                    input_mapping={"status": LiteralBinding(value="approved")},
                ),
            ),
            edges=(PlanEdge(from_node="approve", to_node="after"),),
            outputs={
                "status": NodeOutputBinding(
                    node_id="after",
                    pointer="/output/data/status",
                )
            },
        )
        waiting = await engine.execute(make_request(), plan)

        result = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=waiting.continuation.approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer-1",
                reason="looks good",
            ),
        )
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["status"], "approved")
        self.assertEqual(tool.calls, [{"status": "approved"}])
        self.assertEqual(saved.state.pending_approvals, [])
        self.assertEqual(saved.state.nodes["approve"].status, NodeExecutionStatus.SUCCEEDED)
        approval_result = saved.state.nodes["approve"].result
        self.assertEqual(approval_result.output.data["decision"], "approved")
        self.assertEqual(approval_result.output.data["decided_by"], "reviewer-1")
        self.assertEqual(saved.state.metadata["approval_decisions"][0]["decision"], "approved")

    async def test_rejected_decision_can_follow_denied_edge(self) -> None:
        yes = ApprovalTool("approval.yes/v1")
        no = ApprovalTool("approval.no/v1")
        store = InMemoryStateStore()
        engine = make_engine(yes, no, state_store=store)
        plan = ExecutionPlan(
            plan_id="approval-rejected",
            nodes=(
                approval_node(failure_policy=FailurePolicy.CONTINUE),
                PlanNode(
                    node_id="yes",
                    capability="approval.yes/v1",
                    input_mapping={"branch": LiteralBinding(value="yes")},
                ),
                PlanNode(
                    node_id="no",
                    capability="approval.no/v1",
                    input_mapping={"branch": LiteralBinding(value="no")},
                ),
            ),
            edges=(
                PlanEdge(from_node="approve", to_node="yes", trigger=EdgeTrigger.SUCCESS),
                PlanEdge(from_node="approve", to_node="no", trigger=EdgeTrigger.DENIED),
            ),
            outputs={
                "branch": NodeOutputBinding(
                    node_id="no",
                    pointer="/output/data/branch",
                )
            },
        )
        waiting = await engine.execute(make_request(), plan)

        result = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=waiting.continuation.approval_id,
                decision=ApprovalDecisionType.REJECTED,
                decided_by="reviewer-2",
                reason="risk too high",
            ),
        )
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.output.data["branch"], "no")
        self.assertEqual(yes.calls, [])
        self.assertEqual(no.calls, [{"branch": "no"}])
        self.assertEqual(saved.state.nodes["approve"].status, NodeExecutionStatus.DENIED)
        self.assertEqual(saved.state.nodes["yes"].status, NodeExecutionStatus.SKIPPED)
        self.assertEqual(saved.state.nodes["no"].status, NodeExecutionStatus.SUCCEEDED)
        self.assertEqual(saved.state.nodes["approve"].error.code, "HARNESS.APPROVAL.REJECTED")

    async def test_sqlite_approval_can_be_resolved_after_engine_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "approval.db"
            first_tool = ApprovalTool()
            first_engine = make_engine(
                first_tool,
                state_store=SQLiteStateStore(database),
            )
            plan = ExecutionPlan(
                plan_id="approval-restart",
                nodes=(
                    approval_node(),
                    PlanNode(
                        node_id="after",
                        capability="approval.work/v1",
                        input_mapping={"status": LiteralBinding(value="after-restart")},
                    ),
                ),
                edges=(PlanEdge(from_node="approve", to_node="after"),),
                outputs={
                    "status": NodeOutputBinding(
                        node_id="after",
                        pointer="/output/data/status",
                    )
                },
            )
            waiting = await first_engine.execute(make_request(), plan)

            resumed_tool = ApprovalTool()
            resumed_engine = make_engine(
                resumed_tool,
                state_store=SQLiteStateStore(database),
            )
            result = await resumed_engine.resolve_approval(
                plan.plan_id,
                ApprovalDecision(
                    approval_id=waiting.continuation.approval_id,
                    decision=ApprovalDecisionType.APPROVED,
                    decided_by="restart-reviewer",
                ),
            )
            saved = await SQLiteStateStore(database).load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.output.data["status"], "after-restart")
            self.assertEqual(first_tool.calls, [])
            self.assertEqual(resumed_tool.calls, [{"status": "after-restart"}])
            self.assertEqual(saved.state.pending_approvals, [])
            self.assertEqual(saved.state.nodes["approve"].status, NodeExecutionStatus.SUCCEEDED)

    async def test_resume_repairs_waiting_checkpoint_before_approval_materialization(self) -> None:
        store = InMemoryStateStore()
        engine = make_engine(state_store=store)
        plan = ExecutionPlan(
            plan_id="approval-repair",
            nodes=(PlanNode(node_id="approve", kind=PlanNodeKind.APPROVAL),),
        )
        continuation = Continuation(
            plan_id=plan.plan_id,
            node_id="approve",
            waiting_reason="approval",
        )
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
                        "approve": NodeExecutionState(
                            node_id="approve",
                            status=NodeExecutionStatus.WAITING,
                            attempt=1,
                            result=ResultEnvelope.accepted(continuation),
                            waiting_reason="approval",
                            continuation=continuation,
                        )
                    },
                ),
            )
        )

        result = await engine.resume(plan.plan_id)
        saved = await store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.ACCEPTED)
        self.assertIsNotNone(result.continuation.approval_id)
        self.assertEqual(len(saved.state.pending_approvals), 1)
        self.assertEqual(
            saved.state.pending_approvals[0].approval_id,
            result.continuation.approval_id,
        )
        self.assertEqual(
            saved.state.nodes["approve"].continuation.approval_id,
            result.continuation.approval_id,
        )

    async def test_unknown_or_duplicate_approval_is_rejected_without_reexecution(self) -> None:
        tool = ApprovalTool()
        store = InMemoryStateStore()
        engine = make_engine(tool, state_store=store)
        plan = ExecutionPlan(
            plan_id="approval-duplicate",
            nodes=(approval_node(),),
        )
        waiting = await engine.execute(make_request(), plan)

        missing = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id="unknown-approval",
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer",
            ),
        )
        self.assertEqual(missing.status, ResultStatus.FAILED)
        self.assertEqual(missing.error.code, "HARNESS.APPROVAL.NOT_PENDING")

        first = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=waiting.continuation.approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer",
            ),
        )
        duplicate = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=waiting.continuation.approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer",
            ),
        )

        self.assertEqual(first.status, ResultStatus.SUCCESS)
        self.assertEqual(duplicate.status, ResultStatus.FAILED)
        self.assertEqual(duplicate.error.code, "HARNESS.APPROVAL.NOT_PENDING")
        self.assertEqual(tool.calls, [])

    async def test_parallel_approvals_are_independently_resolvable(self) -> None:
        store = InMemoryStateStore()
        engine = make_engine(state_store=store)
        plan = ExecutionPlan(
            plan_id="parallel-approvals",
            nodes=(
                PlanNode(node_id="a", kind=PlanNodeKind.APPROVAL),
                PlanNode(node_id="b", kind=PlanNodeKind.APPROVAL),
            ),
        )
        waiting = await engine.execute(make_request(), plan)
        initial = await store.load(plan.plan_id)

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(len(initial.state.pending_approvals), 2)
        approvals = {item.node_id: item for item in initial.state.pending_approvals}

        still_waiting = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=approvals["a"].approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer-a",
            ),
        )
        self.assertEqual(still_waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(still_waiting.continuation.approval_id, approvals["b"].approval_id)

        completed = await engine.resolve_approval(
            plan.plan_id,
            ApprovalDecision(
                approval_id=approvals["b"].approval_id,
                decision=ApprovalDecisionType.APPROVED,
                decided_by="reviewer-b",
            ),
        )
        self.assertEqual(completed.status, ResultStatus.SUCCESS)
        self.assertEqual(completed.output.data, {})


if __name__ == "__main__":
    unittest.main()
