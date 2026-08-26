"""HarnessApplication Async WAITING API 生命周期测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import BootstrapStateError, build_harness
from harness_contracts import ResultEnvelope, ResultOutput, ResultStatus


class AsyncWaitingApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_async_node_requires_started_application_and_delegates(self) -> None:
        app = build_harness(entry_point_group=None)
        terminal = ResultEnvelope.success(ResultOutput(type="json", data={}))

        with self.assertRaises(BootstrapStateError):
            await app.complete_async_node("missing-plan", "node", terminal)

        await app.start()
        result = await app.complete_async_node("missing-plan", "node", terminal)
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.NOT_FOUND")
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
