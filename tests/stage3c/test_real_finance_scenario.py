"""Foundation F5 真实财经场景的确定性业务验收。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    ExecutionMode,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultStatus,
)
from portfolio_risk_agent import PORTFOLIO_RISK_CAPABILITY_ID, PortfolioRiskAgentPlugin


def portfolio_snapshot() -> dict[str, object]:
    return {
        "portfolio_id": "real-use-portfolio",
        "as_of": "2026-08-31T16:00:00+08:00",
        "base_currency": "CNY",
        "cash": "1000",
        "positions": [
            {
                "symbol": "AAA",
                "quantity": "10",
                "current_price": "100",
                "previous_close": "105",
            },
            {
                "symbol": "BBB",
                "quantity": "20",
                "current_price": "50",
                "previous_close": "49",
            },
        ],
        "limits": {
            "max_position_weight_pct": "30",
            "max_daily_loss_pct": "0.5",
        },
    }


class PortfolioRiskScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_path_runs_real_business_calculation(self) -> None:
        plugin = PortfolioRiskAgentPlugin()
        app = build_harness(plugins=(plugin,), entry_point_group=None)
        request = Request(
            input=RequestInput(type="portfolio_snapshot", content=portfolio_snapshot()),
            target=RequestTarget(capability=PORTFOLIO_RISK_CAPABILITY_ID),
            options=RequestOptions(execution_mode=ExecutionMode.FAST),
        )

        async with app:
            result = await app.handle(request)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["execution_mode"], "fast")
        self.assertEqual(result.metadata["data_source"], "request_snapshot")
        output = result.output.data
        self.assertEqual(output["valuation"]["net_asset_value"], "3000.00")
        self.assertEqual(output["valuation"]["daily_pnl"], "-30.00")
        self.assertEqual(output["valuation"]["daily_return_pct"], "-0.9901")
        self.assertEqual(output["risk_level"], "high")
        self.assertEqual(
            [item["code"] for item in output["breaches"]],
            ["POSITION_CONCENTRATION", "POSITION_CONCENTRATION", "DAILY_LOSS_LIMIT"],
        )
        self.assertEqual(output["grounding"]["source"], "request_snapshot")
        self.assertTrue(plugin.initialized is False)

    async def test_invalid_snapshot_fails_without_partial_valuation(self) -> None:
        app = build_harness(plugins=(PortfolioRiskAgentPlugin(),), entry_point_group=None)
        request = Request(
            input=RequestInput(
                type="portfolio_snapshot",
                content={**portfolio_snapshot(), "positions": []},
            ),
            target=RequestTarget(capability=PORTFOLIO_RISK_CAPABILITY_ID),
            options=RequestOptions(execution_mode=ExecutionMode.FAST),
        )

        async with app:
            result = await app.handle(request)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.REQUEST.INVALID")
        self.assertIsNone(result.output)
        self.assertEqual(result.error.details["validation_error_count"], 1)


if __name__ == "__main__":
    unittest.main()
