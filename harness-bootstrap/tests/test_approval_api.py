"""HarnessApplication Approval API 生命周期测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import BootstrapStateError, build_harness
from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ExecutionPlan,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestInput,
    ResultStatus,
)


class ApprovalApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_approval_requires_started_application_and_delegates(self) -> None:
        app = build_harness(entry_point_group=None)
        decision = ApprovalDecision(
            approval_id="approval-1",
            decision=ApprovalDecisionType.APPROVED,
            decided_by="reviewer",
        )

        with self.assertRaises(BootstrapStateError):
            await app.resolve_approval("approval-app", decision)

        await app.start()
        waiting = await app.execute_plan(
            Request(input=RequestInput(type="json", content={})),
            ExecutionPlan(
                plan_id="approval-app",
                nodes=(PlanNode(node_id="approve", kind=PlanNodeKind.APPROVAL),),
            ),
        )
        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)

        completed = await app.resolve_approval(
            "approval-app",
            decision.model_copy(update={"approval_id": waiting.continuation.approval_id}),
        )
        self.assertEqual(completed.status, ResultStatus.SUCCESS)
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
