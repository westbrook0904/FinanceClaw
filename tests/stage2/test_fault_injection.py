"""Stage 2 Reliability / Fault Injection acceptance tests."""

from __future__ import annotations

import asyncio
import unittest

from harness_contracts import (
    CapabilityError,
    CapabilityExecutionProfile,
    ExecutionPlan,
    IdempotencyType,
    InvocationContext,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanExecutionRecord,
    PlanExecutionStatus,
    PlanNode,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
)
from harness_execution.cancellation import CancellationSignal
from harness_state import StateStore

from tests.stage2.support import (
    BlockingTool,
    InvalidResultTool,
    ScriptedTool,
    SleepingTool,
    make_engine,
    make_request,
)


def retryable_failure(code: str = "TEST.TRANSIENT") -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "transient injected failure",
            code=code,
            retryable=True,
        ).to_detail()
    )


def permanent_failure(code: str = "TEST.PERMANENT") -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "permanent injected failure",
            code=code,
            retryable=False,
        ).to_detail()
    )


class FailingCreateStateStore(StateStore):
    """模拟首个稳定 checkpoint 无法持久化。"""

    async def create(self, record: PlanExecutionRecord) -> None:
        raise RuntimeError("injected checkpoint create failure")

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        return None

    async def save(self, record: PlanExecutionRecord) -> None:
        raise AssertionError("save should not be reached")

    async def delete(self, plan_id: str) -> None:
        return None


class FaultInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_retries_and_succeeds(self) -> None:
        tool = ScriptedTool(
            "fault.transient/v1",
            (
                retryable_failure(),
                ResultEnvelope.success(ResultOutput(type="json", data={"value": 7})),
            ),
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-transient",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="fault.transient/v1",
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
            outputs={
                "value": NodeOutputBinding(
                    node_id="work",
                    pointer="/output/data/value",
                )
            },
        )

        result = await fixture.engine.execute(make_request(), plan)
        saved = await fixture.state_store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["value"], 7)
        self.assertEqual(tool.calls, 2)
        self.assertEqual(saved.state.nodes["work"].attempt, 2)
        self.assertEqual(saved.state.nodes["work"].status, NodeExecutionStatus.SUCCEEDED)

    async def test_retry_exhausted_returns_final_failure(self) -> None:
        tool = ScriptedTool(
            "fault.exhaust/v1",
            (retryable_failure("TEST.RETRY.EXHAUSTED"),),
            profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
        )
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-retry-exhausted",
            nodes=(
                PlanNode(
                    node_id="work",
                    capability="fault.exhaust/v1",
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await fixture.engine.execute(make_request(), plan)
        saved = await fixture.state_store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "TEST.RETRY.EXHAUSTED")
        self.assertEqual(tool.calls, 3)
        self.assertEqual(saved.state.nodes["work"].attempt, 3)
        self.assertEqual(saved.state.status, PlanExecutionStatus.FAILED)

    async def test_unsafe_write_does_not_retry_retryable_failure(self) -> None:
        tool = ScriptedTool(
            "fault.unsafe-write/v1",
            (retryable_failure("TEST.UNSAFE.WRITE"),),
            profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.NONE,
            ),
        )
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-unsafe-write",
            nodes=(
                PlanNode(
                    node_id="write",
                    capability="fault.unsafe-write/v1",
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
        )

        result = await fixture.engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "TEST.UNSAFE.WRITE")
        self.assertEqual(tool.calls, 1)

    async def test_node_timeout_stops_provider(self) -> None:
        tool = SleepingTool("fault.timeout/v1", delay_seconds=1)
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-timeout",
            nodes=(
                PlanNode(
                    node_id="slow",
                    capability="fault.timeout/v1",
                    timeout_ms=20,
                ),
            ),
        )

        result = await fixture.engine.execute(make_request(), plan)
        saved = await fixture.state_store.load(plan.plan_id)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.TIMEOUT")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(saved.state.nodes["slow"].status, NodeExecutionStatus.FAILED)

    async def test_cancel_before_scheduler_run_never_calls_provider(self) -> None:
        tool = ScriptedTool(
            "fault.cancel-before/v1",
            (ResultEnvelope.success(ResultOutput(type="json", data={"ok": True})),),
        )
        fixture = make_engine(tool)
        request = make_request("cancel-before-request")
        plan = ExecutionPlan(
            plan_id="fault-cancel-before",
            nodes=(PlanNode(node_id="work", capability="fault.cancel-before/v1"),),
        )
        signal = CancellationSignal()
        self.assertTrue(signal.request("cancel before run"))

        outcome = await fixture.scheduler.run(
            request,
            plan,
            InvocationContext(request=request),
            parent=None,
            trace_enabled=False,
            cancellation=signal,
        )

        self.assertEqual(outcome.result.status, ResultStatus.CANCELLED)
        self.assertEqual(outcome.state.status, PlanExecutionStatus.CANCELLED)
        self.assertEqual(outcome.state.nodes["work"].status, NodeExecutionStatus.CANCELLED)
        self.assertEqual(tool.calls, 0)

    async def test_cancel_while_running_cancels_node_and_plan(self) -> None:
        tool = BlockingTool("fault.cancel-running/v1")
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-cancel-running",
            nodes=(PlanNode(node_id="work", capability="fault.cancel-running/v1"),),
        )

        execution = asyncio.create_task(fixture.engine.execute(make_request(), plan))
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        accepted = await fixture.engine.cancel(plan.plan_id, "user requested stop")
        result = await asyncio.wait_for(execution, timeout=1)
        saved = await fixture.state_store.load(plan.plan_id)

        self.assertTrue(accepted)
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertEqual(saved.state.status, PlanExecutionStatus.CANCELLED)
        self.assertEqual(saved.state.nodes["work"].status, NodeExecutionStatus.CANCELLED)
        self.assertTrue(saved.context.cancellation.cancelled)

    async def test_provider_exception_is_normalized(self) -> None:
        tool = ScriptedTool(
            "fault.exception/v1",
            (RuntimeError("injected provider crash"),),
        )
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-provider-exception",
            nodes=(PlanNode(node_id="work", capability="fault.exception/v1"),),
        )

        result = await fixture.engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.CAPABILITY.EXECUTION_FAILED")
        self.assertEqual(tool.calls, 1)

    async def test_invalid_provider_result_is_rejected(self) -> None:
        tool = InvalidResultTool("fault.invalid-result/v1")
        fixture = make_engine(tool)
        plan = ExecutionPlan(
            plan_id="fault-invalid-result",
            nodes=(PlanNode(node_id="work", capability="fault.invalid-result/v1"),),
        )

        result = await fixture.engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.CAPABILITY.INVALID_RESULT")
        self.assertEqual(tool.calls, 1)

    async def test_checkpoint_failure_prevents_provider_side_effect(self) -> None:
        tool = ScriptedTool(
            "fault.checkpoint/v1",
            (ResultEnvelope.success(ResultOutput(type="json", data={"ok": True})),),
        )
        fixture = make_engine(tool, state_store=FailingCreateStateStore())
        plan = ExecutionPlan(
            plan_id="fault-checkpoint",
            nodes=(PlanNode(node_id="work", capability="fault.checkpoint/v1"),),
        )

        result = await fixture.engine.execute(make_request(), plan)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.EXECUTION_FAILED")
        self.assertEqual(tool.calls, 0)


if __name__ == "__main__":
    unittest.main()
