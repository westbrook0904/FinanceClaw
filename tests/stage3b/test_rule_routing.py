"""RuleRouter deterministic-first、no-match 与 Validator 权威性 Gate。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import ExecutionMode, ResultStatus
from harness_routing import InputTypeRouteRule, RuleRouter

from .support import AcceptancePlugin, RecordingTool, ScriptedRouter, fast_decision, make_request


class RuleRoutingAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_target_wins_and_never_calls_fallback(self) -> None:
        tool = RecordingTool()
        fallback = ScriptedRouter(AssertionError("fallback must not run"))
        router = RuleRouter(
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="stage3b-goal",
                    mode=ExecutionMode.PLAN,
                ),
            ),
            fallback=fallback,
        )
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            router=router,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request(target=True))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["route_reason_code"], "EXPLICIT_TARGET")
        self.assertEqual(len(fallback.contexts), 0)
        self.assertEqual(tool.calls, 1)

    async def test_input_type_rule_routes_auto_to_fast(self) -> None:
        tool = RecordingTool()
        router = RuleRouter(
            input_type_rules=(
                InputTypeRouteRule(
                    input_type="stage3b-goal",
                    mode=ExecutionMode.FAST,
                    capability_id=tool.descriptor().id,
                    reason_code="ACCEPTANCE_FAST_RULE",
                ),
            )
        )
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            router=router,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["route_reason_code"], "ACCEPTANCE_FAST_RULE")
        self.assertEqual(tool.calls, 1)

    async def test_no_match_is_explicit_and_invokes_nothing(self) -> None:
        tool = RecordingTool()
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            router=RuleRouter(),
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ROUTE.NO_MATCH")
        self.assertEqual(tool.calls, 0)

    async def test_unknown_fast_capability_is_rejected_before_invocation(self) -> None:
        tool = RecordingTool()
        router = ScriptedRouter(fast_decision(capability_id="missing.stage3b/v1"))
        app = build_harness(
            plugins=(AcceptancePlugin((tool,)),),
            router=router,
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ROUTE.INVALID_DECISION")
        self.assertEqual(tool.calls, 0)


if __name__ == "__main__":
    unittest.main()
