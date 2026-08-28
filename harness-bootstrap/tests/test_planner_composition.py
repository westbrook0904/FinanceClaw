"""Stage 3B Step 5 PlannerRegistry 的 Composition Root 接线测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    ErrorCode,
    ExecutionMode,
    ExecutionPlan,
    PlanningError,
    PlanNode,
    Request,
    RequestInput,
    RequestOptions,
    ResultStatus,
)
from harness_planning import Planner, PlannerRegistry, PlanningContext
from harness_routing import RuleRouter


class RecordingPlanner(Planner):
    def __init__(self, planner_id: str) -> None:
        self._planner_id = planner_id
        self.calls = 0

    @property
    def planner_id(self) -> str:
        return self._planner_id

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        self.calls += 1
        return ExecutionPlan(
            plan_id="not-executed-in-step-5",
            nodes=(PlanNode(node_id="node", capability="tool/v1"),),
        )


class PlannerCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_wires_registry_default_router_and_validator(self) -> None:
        planner = RecordingPlanner("static")
        app = build_harness(
            planners=(planner,),
            default_planner_id="static",
            entry_point_group=None,
        )

        self.assertIsInstance(app.planner_registry, PlannerRegistry)
        self.assertIs(app.planner_registry.get("static"), planner)
        self.assertEqual(app.planner_registry.list(), ("static",))
        self.assertEqual(app.route_decision_validator.planner_ids, frozenset({"static"}))
        self.assertIsInstance(app.router, RuleRouter)

        await app.start()
        result = await app.handle(
            Request(
                input=RequestInput(type="goal", content={"task": "plan"}),
                options=RequestOptions(execution_mode=ExecutionMode.PLAN),
            )
        )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, ErrorCode.ROUTE_MODE_NOT_AVAILABLE)
        self.assertEqual(result.error.details["planner_id"], "static")
        self.assertEqual(planner.calls, 0)
        await app.shutdown()

    async def test_default_planner_must_be_registered(self) -> None:
        with self.assertRaises(PlanningError) as raised:
            build_harness(
                default_planner_id="missing",
                entry_point_group=None,
            )

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_NOT_CONFIGURED)

    async def test_duplicate_planner_ids_fail_during_build(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate planner_id"):
            build_harness(
                planners=(RecordingPlanner("same"), RecordingPlanner("same")),
                entry_point_group=None,
            )


if __name__ == "__main__":
    unittest.main()
