"""PRE_ROUTE deny、forced mode 与 route scope 的仓库级 Gate。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import ExecutionMode, ResultStatus
from harness_events import ExecutionEventName
from harness_policy import Policy, PolicyContext, PolicyDecision, PolicyPhase

from .support import (
    AcceptancePlugin,
    RecordingTool,
    ScriptedPlanner,
    ScriptedRouter,
    echo_plan,
    fast_decision,
    make_request,
)


class PreRoutePolicy(Policy):
    def __init__(
        self,
        *,
        deny: bool = False,
        constraints: dict[str, object] | None = None,
    ) -> None:
        self.deny = deny
        self.constraints = constraints or {}
        self.calls: list[PolicyContext] = []

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_ROUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        self.calls.append(context)
        if self.deny:
            return PolicyDecision.deny(self.name, reason="blocked at pre-route")
        return PolicyDecision.allow(
            self.name,
            reason="constrained at pre-route",
            constraints=self.constraints,
        )


class PolicyAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_route_deny_stops_router_planner_and_capability(self) -> None:
        policy = PreRoutePolicy(deny=True)
        router = ScriptedRouter(fast_decision())
        planner = ScriptedPlanner(echo_plan())
        tool = RecordingTool()
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            policies=(policy,),
            router=router,
            planners=(planner,),
            default_planner_id=planner.planner_id,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request(target=True))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(result.error.code, "HARNESS.POLICY.DENIED")
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(len(router.contexts), 0)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(tool.calls, 0)

    async def test_policy_forces_auto_to_plan_and_records_policy_source(self) -> None:
        policy = PreRoutePolicy(constraints={"forced_mode": "plan"})
        planner = ScriptedPlanner(echo_plan())
        tool = RecordingTool()
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            policies=(policy,),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["execution_mode"], "plan")
        self.assertEqual(planner.calls, 1)
        self.assertEqual(tool.calls, 1)
        selected = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.MODE_SELECTED
        )
        self.assertEqual(selected.attributes["source"], "policy")

    async def test_router_cannot_violate_policy_forced_mode(self) -> None:
        policy = PreRoutePolicy(constraints={"forced_mode": "plan"})
        router = ScriptedRouter(fast_decision())
        planner = ScriptedPlanner(echo_plan())
        tool = RecordingTool()
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            policies=(policy,),
            router=router,
            planners=(planner,),
            default_planner_id=planner.planner_id,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ROUTE.MODE_NOT_ALLOWED")
        self.assertEqual(planner.calls, 0)
        self.assertEqual(tool.calls, 0)

    async def test_capability_and_planner_scope_fail_closed(self) -> None:
        cases = (
            ({"allowed_capability_ids": ["other.tool/v1"]}, "capability"),
            ({"allowed_planner_ids": ["other-planner"]}, "planner"),
        )
        for constraints, boundary in cases:
            with self.subTest(boundary=boundary):
                policy = PreRoutePolicy(constraints=constraints)
                planner = ScriptedPlanner(echo_plan())
                tool = RecordingTool()
                app = build_harness(
                    plugins=(AcceptancePlugin((tool,)),),
                    policies=(policy,),
                    planners=(planner,),
                    default_planner_id=planner.planner_id,
                    entry_point_group=None,
                )
                await app.start()
                try:
                    result = await app.handle(
                        make_request(
                            target=boundary == "capability",
                            mode=(
                                ExecutionMode.AUTO
                                if boundary == "capability"
                                else ExecutionMode.PLAN
                            ),
                        )
                    )
                finally:
                    await app.shutdown()

                expected = (
                    "HARNESS.ROUTE.CAPABILITY_NOT_ALLOWED"
                    if boundary == "capability"
                    else "HARNESS.ROUTE.PLANNER_NOT_ALLOWED"
                )
                self.assertEqual(result.status, ResultStatus.FAILED)
                self.assertEqual(result.error.code, expected)
                self.assertEqual(planner.calls, 0)
                self.assertEqual(tool.calls, 0)


if __name__ == "__main__":
    unittest.main()
