"""Foundation F5 Real-use Gate 的离线全链路验收。"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from financeclaw_real_use.gate import default_portfolio_snapshot, run_live_gate
from harness_model import JsonHttpResponse
from portfolio_risk_agent import PORTFOLIO_RISK_CAPABILITY_ID


class GateScriptTransport:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, object]] = []

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> JsonHttpResponse:
        del url, headers, timeout_seconds
        self.calls.append(payload)
        ordinal = len(self.calls)
        if ordinal == 1:
            output = self._plan()
        elif ordinal == 2:
            output = {
                "kind": "call_capability",
                "capability_id": PORTFOLIO_RISK_CAPABILITY_ID,
                "input": {
                    "type": "portfolio_snapshot",
                    "content": {
                        **default_portfolio_snapshot(include_limits=False),
                        "limits": {
                            "max_position_weight_pct": "30",
                            "max_daily_loss_pct": "0.5",
                        },
                    },
                },
                "reason_code": "APPLY_REMEMBERED_RISK_LIMITS",
            }
        elif ordinal == 3:
            observation_id = self._observation_id(payload)
            output = {
                "kind": "finish",
                "output": {
                    "type": "portfolio_review_summary",
                    "data": {"status": "reviewed", "source": "completed_observation"},
                },
                "evidence_refs": [observation_id],
                "reason_code": "RISK_REVIEW_COMPLETE",
            }
        else:
            raise AssertionError(f"unexpected model call: {ordinal}")
        return JsonHttpResponse(
            status_code=200,
            body={
                "id": f"resp_gate_{ordinal}",
                "model": "gate-test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output, separators=(",", ":")),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    @staticmethod
    def _plan() -> dict[str, object]:
        fields = (
            "portfolio_id",
            "as_of",
            "base_currency",
            "cash",
            "positions",
            "limits",
        )
        return {
            "nodes": [
                {
                    "node_id": "portfolio-risk-review",
                    "capability_id": PORTFOLIO_RISK_CAPABILITY_ID,
                    "input_mapping": {
                        field: {
                            "kind": "request",
                            "pointer": f"/input/content/{field}",
                        }
                        for field in fields
                    },
                }
            ],
            "edges": [],
            "outputs": {
                "review": {
                    "kind": "node_output",
                    "node_id": "portfolio-risk-review",
                    "pointer": "/output/data",
                }
            },
        }

    @staticmethod
    def _observation_id(payload: Mapping[str, object]) -> str:
        messages = payload["input"]
        prompt = json.loads(messages[-1]["content"])
        for item in prompt["context"]["items"]:
            if item["source_kind"] == "observation":
                return item["content"]["observation_id"]
        raise AssertionError("second Explore turn did not include an Observation")


class RealUseGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_transport_proves_gate_wiring_without_claiming_live(self) -> None:
        transport = GateScriptTransport()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            report = await run_live_gate(
                output_dir=output_dir,
                api_key="offline-test-secret",
                openai_model="gate-test-model",
                transport=transport,
                live=False,
            )
            persisted = json.loads(
                (output_dir / "real-use-report.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(transport.calls), 3)
        self.assertFalse(report["live"])
        self.assertTrue(report["checks_passed"])
        self.assertFalse(report["gate_passed"])
        self.assertEqual(report["metrics"]["groundedness_passes"], 3)
        self.assertTrue(report["metrics"]["memory_context_hit"])
        self.assertTrue(report["metrics"]["memory_applied"])
        self.assertEqual(report["metrics"]["repeated_action_count"], 0)
        self.assertEqual(report["runs"]["fast"]["model_span_count"], 0)
        self.assertGreaterEqual(report["runs"]["plan"]["model_span_count"], 1)
        self.assertEqual(report["runs"]["explore"]["action_span_count"], 1)
        self.assertEqual(persisted["schema_version"], "financeclaw-real-use-gate-v1")
        self.assertNotIn("offline-test-secret", json.dumps(persisted))

    async def test_non_live_run_requires_explicit_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "explicit test transport"):
                await run_live_gate(
                    output_dir=Path(directory),
                    api_key="offline-test-secret",
                    openai_model="gate-test-model",
                    live=False,
                )


if __name__ == "__main__":
    unittest.main()
