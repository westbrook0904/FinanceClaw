"""HarnessApplication Resume API 生命周期测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import BootstrapStateError, build_harness
from harness_contracts import ResultStatus


class ResumeApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_plan_requires_started_application(self) -> None:
        app = build_harness(entry_point_group=None)

        with self.assertRaises(BootstrapStateError):
            await app.resume_plan("missing-plan")

        await app.start()
        result = await app.resume_plan("missing-plan")

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.NOT_FOUND")
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
