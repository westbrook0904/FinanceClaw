"""Stage 2 public-API end-to-end acceptance scenarios."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from calculator_tool import CalculatorToolPlugin
from echo_agent import EchoAgentPlugin
from mock_finance_agent import MockFinanceAgentPlugin

from harness_bootstrap import build_harness
from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    CapabilityError,
    CapabilityExecutionProfile,
    EdgeTrigger,
    ExecutionPlan,
    FailurePolicy,
    LiteralBinding,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanBudget,
    PlanEdge,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    RequestInput,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
)
from harness_state import SQLiteStateStore

from tests.stage2.support import EchoTool, ScriptedTool, TestPlugin


def retryable_failure() -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "injected transient failure",
            code="E2E.TRANSIENT",
            retryable=True,
        ).to_detail()
    )


def permanent_failure() -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "injected permanent failure",
            code="E2E.PERMANENT",
        ).to_detail()
    )


class Stage2EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage1_direct_invocation_remains_compatible(self) -> None:
        """Stage 2 不能以破坏 Stage 1 Direct Invocation 为代价。"""

        request = Request(
            request_id="stage1-compatibility",
            target=RequestTarget(capability="echo.reply/v1"),
            input=RequestInput(type="text", content="still-direct"),
        )
        async with build_harness(
            plugins=(EchoAgentPlugin(),),
            entry_point_group=None,
        ) as app:
            result = await app.invoke(request)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.type, "text")
        self.assertEqual(result.output.data, "still-direct")

    async def test_real_finance_review_plan_restart_approval_resume(self) -> None:
        """按说明书 finance-review-plan 走真实内置 Plugin + SQLite restart。"""

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "finance-review.db"
            plan = ExecutionPlan(
                plan_id="finance-review-plan",
                budget=PlanBudget(max_concurrency=2),
                nodes=(
                    PlanNode(
                        node_id="n1",
                        capability="finance.mock-query/v1",
                        input_mapping={
                            "query": RequestBinding(pointer="/input/content/query")
                        },
                    ),
                    PlanNode(
                        node_id="n2",
                        capability="math.calculate/v1",
                        input_mapping={
                            "operation": LiteralBinding(value="add"),
                            "left": LiteralBinding(value=2),
                            "right": LiteralBinding(value=3),
                        },
                    ),
                    PlanNode(node_id="n3", kind=PlanNodeKind.APPROVAL),
                    PlanNode(
                        node_id="n4",
                        capability="echo.reply/v1",
                        input_mapping={
                            "finance_message": NodeOutputBinding(
                                node_id="n1",
                                pointer="/output/data/message",
                            ),
                            "calculated": NodeOutputBinding(
                                node_id="n2",
                                pointer="/output/data",
                            ),
                        },
                    ),
                ),
                edges=(
                    PlanEdge(from_node="n1", to_node="n3"),
                    PlanEdge(from_node="n2", to_node="n3"),
                    PlanEdge(from_node="n3", to_node="n4"),
                ),
                outputs={
                    "finance_message": NodeOutputBinding(
                        node_id="n4",
                        pointer="/output/data/finance_message",
                    ),
                    "calculated": NodeOutputBinding(
                        node_id="n4",
                        pointer="/output/data/calculated",
                    ),
                },
            )
            request = Request(
                request_id="finance-review-request",
                input=RequestInput(type="json", content={"query": "review ACME"}),
            )

            async with build_harness(
                plugins=(
                    MockFinanceAgentPlugin(),
                    CalculatorToolPlugin(),
                    EchoAgentPlugin(),
                ),
                entry_point_group=None,
                state_store=SQLiteStateStore(database),
            ) as first_app:
                waiting = await first_app.execute_plan(request, plan)
                waiting_state = await first_app.state_store.load(plan.plan_id)

                self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
                self.assertIsNotNone(waiting.continuation.approval_id)
                self.assertEqual(waiting.continuation.node_id, "n3")
                self.assertEqual(waiting_state.state.status, PlanExecutionStatus.WAITING)
                self.assertEqual(
                    waiting_state.state.nodes["n1"].status,
                    NodeExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(
                    waiting_state.state.nodes["n2"].status,
                    NodeExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(
                    waiting_state.state.nodes["n3"].status,
                    NodeExecutionStatus.WAITING,
                )
                self.assertEqual(
                    waiting_state.state.nodes["n4"].status,
                    NodeExecutionStatus.PENDING,
                )

            async with build_harness(
                plugins=(
                    MockFinanceAgentPlugin(),
                    CalculatorToolPlugin(),
                    EchoAgentPlugin(),
                ),
                entry_point_group=None,
                state_store=SQLiteStateStore(database),
            ) as restarted_app:
                result = await restarted_app.resolve_approval(
                    plan.plan_id,
                    ApprovalDecision(
                        approval_id=waiting.continuation.approval_id,
                        decision=ApprovalDecisionType.APPROVED,
                        decided_by="stage2-e2e-reviewer",
                    ),
                )
                final_state = await restarted_app.state_store.load(plan.plan_id)

                self.assertEqual(result.status, ResultStatus.SUCCESS)
                self.assertEqual(result.output.data["calculated"], 5)
                self.assertEqual(
                    result.output.data["finance_message"],
                    "mock finance agent executed",
                )
                self.assertEqual(result.trace_id, waiting.trace_id)
                self.assertEqual(final_state.state.status, PlanExecutionStatus.SUCCEEDED)
                self.assertTrue(
                    all(
                        node.status is NodeExecutionStatus.SUCCEEDED
                        for node in final_state.state.nodes.values()
                    )
                )

    async def test_retry_continue_partial_restart_approval_then_finish(self) -> None:
        """说明书故障 E2E：Retry success + CONTINUE PARTIAL + restart + Approval。"""

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "faulted-e2e.db"
            read_profile = CapabilityExecutionProfile(side_effect=SideEffectType.READ)
            first_n1 = ScriptedTool(
                "e2e.retry/v1",
                (
                    retryable_failure(),
                    ResultEnvelope.success(
                        ResultOutput(type="json", data={"value": "recovered"})
                    ),
                ),
                profile=read_profile,
            )
            first_n2 = ScriptedTool(
                "e2e.partial/v1",
                (permanent_failure(),),
                profile=read_profile,
            )
            first_n4 = EchoTool("e2e.echo/v1")
            first_plugin = TestPlugin(
                "stage2-faulted-e2e-first",
                (first_n1, first_n2, first_n4),
            )
            plan = ExecutionPlan(
                plan_id="stage2-faulted-restart",
                budget=PlanBudget(max_concurrency=2),
                nodes=(
                    PlanNode(
                        node_id="n1",
                        capability="e2e.retry/v1",
                        retry_policy=RetryPolicy(
                            max_attempts=2,
                            initial_backoff_ms=0,
                            max_backoff_ms=0,
                        ),
                    ),
                    PlanNode(
                        node_id="n2",
                        capability="e2e.partial/v1",
                        failure_policy=FailurePolicy.CONTINUE,
                    ),
                    PlanNode(node_id="n3", kind=PlanNodeKind.APPROVAL),
                    PlanNode(
                        node_id="n4",
                        capability="e2e.echo/v1",
                        input_mapping={
                            "value": NodeOutputBinding(
                                node_id="n1",
                                pointer="/output/data/value",
                            )
                        },
                    ),
                ),
                edges=(
                    PlanEdge(from_node="n1", to_node="n3", trigger=EdgeTrigger.SUCCESS),
                    PlanEdge(from_node="n2", to_node="n3", trigger=EdgeTrigger.ALWAYS),
                    PlanEdge(from_node="n3", to_node="n4", trigger=EdgeTrigger.SUCCESS),
                ),
                outputs={
                    "value": NodeOutputBinding(
                        node_id="n4",
                        pointer="/output/data/value",
                    )
                },
            )
            request = Request(
                request_id="stage2-faulted-e2e-request",
                input=RequestInput(type="json", content={}),
            )

            async with build_harness(
                plugins=(first_plugin,),
                entry_point_group=None,
                state_store=SQLiteStateStore(database),
            ) as first_app:
                waiting = await first_app.execute_plan(request, plan)
                checkpoint = await first_app.state_store.load(plan.plan_id)

                self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
                self.assertEqual(first_n1.calls, 2)
                self.assertEqual(first_n2.calls, 1)
                self.assertEqual(first_n4.calls, 0)
                self.assertEqual(
                    checkpoint.state.nodes["n1"].status,
                    NodeExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(
                    checkpoint.state.nodes["n2"].status,
                    NodeExecutionStatus.FAILED,
                )
                self.assertEqual(len(checkpoint.state.issues), 1)

            restarted_n1 = ScriptedTool(
                "e2e.retry/v1",
                (
                    ResultEnvelope.success(
                        ResultOutput(type="json", data={"value": "must-not-rerun"})
                    ),
                ),
                profile=read_profile,
            )
            restarted_n2 = ScriptedTool(
                "e2e.partial/v1",
                (permanent_failure(),),
                profile=read_profile,
            )
            restarted_n4 = EchoTool("e2e.echo/v1")
            restarted_plugin = TestPlugin(
                "stage2-faulted-e2e-restart",
                (restarted_n1, restarted_n2, restarted_n4),
            )

            async with build_harness(
                plugins=(restarted_plugin,),
                entry_point_group=None,
                state_store=SQLiteStateStore(database),
            ) as restarted_app:
                result = await restarted_app.resolve_approval(
                    plan.plan_id,
                    ApprovalDecision(
                        approval_id=waiting.continuation.approval_id,
                        decision=ApprovalDecisionType.APPROVED,
                        decided_by="stage2-fault-reviewer",
                    ),
                )
                final_state = await restarted_app.state_store.load(plan.plan_id)

                self.assertEqual(result.status, ResultStatus.PARTIAL)
                self.assertEqual(result.output.data["value"], "recovered")
                self.assertEqual(len(result.issues), 1)
                self.assertEqual(result.issues[0].error.code, "E2E.PERMANENT")
                self.assertEqual(restarted_n1.calls, 0)
                self.assertEqual(restarted_n2.calls, 0)
                self.assertEqual(restarted_n4.calls, 1)
                self.assertEqual(final_state.state.status, PlanExecutionStatus.PARTIAL)
                self.assertEqual(
                    final_state.state.nodes["n3"].status,
                    NodeExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(
                    final_state.state.nodes["n4"].status,
                    NodeExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(result.trace_id, waiting.trace_id)


if __name__ == "__main__":
    unittest.main()
