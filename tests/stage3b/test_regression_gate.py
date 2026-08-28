"""Stage 1 兼容性与 Router/Planner 无执行权边界 Gate。"""

from __future__ import annotations

import unittest
from pathlib import Path

from echo_agent import EchoAgentPlugin
from harness_bootstrap import build_harness
from harness_contracts import Request, RequestInput, RequestTarget, ResultStatus


class RegressionAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage1_direct_invocation_requires_no_plugin_changes(self) -> None:
        app = build_harness(
            plugins=(EchoAgentPlugin(),),
            entry_point_group=None,
        )
        request = Request(
            request_id="stage3b-stage1-regression",
            input=RequestInput(type="text", content="legacy-compatible"),
            target=RequestTarget(capability="echo.reply/v1"),
        )
        await app.start()
        try:
            result = await app.invoke(request)
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data, "legacy-compatible")

    async def test_router_and_planner_sources_have_no_execution_or_provider_spi(self) -> None:
        root = Path(__file__).parents[2]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for package in ("harness-routing", "harness-planning")
            for path in sorted((root / package / "src").rglob("*.py"))
        )
        for forbidden in (
            "CapabilityInvoker",
            "ExecutionEngine",
            "ModelProvider",
            "ProviderRegistration",
            "harness_execution",
            "harness_state",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
