"""Stage 1 Direct Invocation 在 Provider Fabric 下的兼容性 Gate。"""

from __future__ import annotations

import unittest

from echo_agent import EchoAgentPlugin
from harness_bootstrap import build_harness
from harness_contracts import Request, RequestInput, RequestTarget, ResultStatus


class LegacyRegressionAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_plugin_direct_invocation_requires_no_provider_changes(self) -> None:
        app = build_harness(
            plugins=(EchoAgentPlugin(),),
            entry_point_group=None,
        )
        request = Request(
            request_id="stage3a-legacy-direct",
            input=RequestInput(type="text", content="legacy-compatible"),
            target=RequestTarget(capability="echo.reply/v1"),
        )

        await app.start()
        try:
            registration = app.registry.get_provider("echo-agent:echo.reply/v1")
            self.assertIsNotNone(registration)
            result = await app.invoke(request)
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data, "legacy-compatible")
        self.assertIsNone(app.registry.get_provider("echo-agent:echo.reply/v1"))
