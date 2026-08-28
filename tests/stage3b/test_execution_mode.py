"""ExecutionMode contract、forced mode 与 3C 模式 fail-closed Gate。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import ExecutionMode, Request, RequestOptions, ResultStatus

from .support import (
    AcceptancePlugin,
    RecordingTool,
    ScriptedPlanner,
    echo_plan,
    make_request,
)


class ExecutionModeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_request_defaults_to_auto_and_all_modes_round_trip(self) -> None:
        legacy = Request.model_validate(
            {
                "request_id": "stage3b-legacy-request",
                "input": {"type": "json", "content": {"value": 1}},
            }
        )
        self.assertEqual(legacy.options.execution_mode, ExecutionMode.AUTO)

        for mode in ExecutionMode:
            with self.subTest(mode=mode):
                options = RequestOptions(execution_mode=mode)
                restored = RequestOptions.model_validate(options.model_dump(mode="json"))
                self.assertEqual(restored.execution_mode, mode)

    async def test_auto_explicit_target_and_forced_fast_dispatch_once(self) -> None:
        tool = RecordingTool()
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            entry_point_group=None,
        )
        await app.start()
        try:
            automatic = await app.handle(make_request(target=True))
            forced = await app.handle(
                make_request(target=True, request_id="stage3b-forced-fast"),
                mode=ExecutionMode.FAST,
            )
        finally:
            await app.shutdown()

        self.assertEqual(automatic.status, ResultStatus.SUCCESS)
        self.assertEqual(automatic.metadata["execution_mode"], "fast")
        self.assertEqual(forced.status, ResultStatus.SUCCESS)
        self.assertEqual(forced.metadata["execution_mode"], "fast")
        self.assertEqual(tool.calls, 2)

    async def test_forced_plan_dispatches_configured_planner(self) -> None:
        tool = RecordingTool()
        planner = ScriptedPlanner(echo_plan())
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request(), mode="plan")
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["execution_mode"], "plan")
        self.assertEqual(planner.calls, 1)
        self.assertEqual(tool.calls, 1)

    async def test_explore_and_hybrid_fail_closed_before_planning_or_execution(self) -> None:
        for mode in (ExecutionMode.EXPLORE, ExecutionMode.HYBRID):
            with self.subTest(mode=mode):
                tool = RecordingTool()
                planner = ScriptedPlanner(echo_plan())
                app = build_harness(
                    plugins=(AcceptancePlugin((tool,)),),
                    planners=(planner,),
                    default_planner_id=planner.planner_id,
                    entry_point_group=None,
                )
                await app.start()
                try:
                    result = await app.handle(make_request(mode=mode))
                finally:
                    await app.shutdown()

                self.assertEqual(result.status, ResultStatus.FAILED)
                self.assertEqual(result.error.code, "HARNESS.ROUTE.MODE_NOT_AVAILABLE")
                self.assertEqual(planner.calls, 0)
                self.assertEqual(tool.calls, 0)


if __name__ == "__main__":
    unittest.main()
