"""Stage 3A checkpointed Provider crash/restart 验收。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityExecutionProfile,
    ExecutionPlan,
    IdempotencyType,
    NodeExecutionStatus,
    PlanNode,
    ResultStatus,
    SideEffectType,
)
from harness_events import ExecutionEventName
from harness_state import SQLiteStateStore
from harness_trace import SpanType

from tests.stage3a.support import AcceptanceProviderTool, make_request, register_provider


class ProviderRestartAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_resume_replays_original_write_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability_id = "acceptance.restart-write/v1"
            profile = CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.REQUIRED,
            )
            started = asyncio.Event()
            live_primary = AcceptanceProviderTool(
                capability_id,
                "live-primary",
                profile=profile,
                started=started,
                block=True,
            )
            live_backup = AcceptanceProviderTool(
                capability_id,
                "live-backup",
                profile=profile,
            )
            live_store = SQLiteStateStore(Path(directory) / "live.db")
            live_app = build_harness(
                entry_point_group=None,
                state_store=live_store,
            )
            register_provider(
                live_app.registry,
                live_primary,
                provider_id="restart-primary",
                priority=100,
                equivalence_group="payment-prod",
            )
            register_provider(
                live_app.registry,
                live_backup,
                provider_id="restart-backup",
                priority=10,
                equivalence_group="payment-prod",
            )
            plan = ExecutionPlan(
                plan_id="stage3a-crash-resume",
                nodes=(
                    PlanNode(
                        node_id="write",
                        capability=capability_id,
                        idempotency_key="payment-acceptance-42",
                    ),
                ),
            )

            await live_app.start()
            execution = asyncio.create_task(
                live_app.execute_plan(make_request("stage3a-restart-request"), plan)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            checkpoint = await live_store.load(plan.plan_id)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(
                checkpoint.state.nodes["write"].status,
                NodeExecutionStatus.RUNNING,
            )
            self.assertEqual(
                checkpoint.state.nodes["write"].selected_provider_id,
                "restart-primary",
            )
            self.assertEqual(
                checkpoint.state.nodes["write"].provider_history[-1].provider_id,
                "restart-primary",
            )

            restart_store = SQLiteStateStore(Path(directory) / "restart.db")
            await restart_store.create(checkpoint)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution
            await live_app.shutdown()

            resumed_primary = AcceptanceProviderTool(
                capability_id,
                "resumed-primary",
                profile=profile,
            )
            higher_priority_backup = AcceptanceProviderTool(
                capability_id,
                "higher-priority-backup",
                profile=profile,
            )
            restarted_app = build_harness(
                entry_point_group=None,
                state_store=restart_store,
            )
            register_provider(
                restarted_app.registry,
                resumed_primary,
                provider_id="restart-primary",
                priority=1,
                equivalence_group="payment-prod",
            )
            register_provider(
                restarted_app.registry,
                higher_priority_backup,
                provider_id="restart-backup",
                priority=200,
                equivalence_group="payment-prod",
            )

            await restarted_app.start()
            try:
                result = await restarted_app.resume_plan(plan.plan_id)
                saved = await restart_store.load(plan.plan_id)
            finally:
                await restarted_app.shutdown()

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(resumed_primary.calls, 1)
            self.assertEqual(higher_priority_backup.calls, 0)
            self.assertEqual(
                saved.state.nodes["write"].result.output.data["provider"],
                "resumed-primary",
            )
            self.assertTrue(
                all(
                    attempt.provider_id == "restart-primary"
                    for attempt in saved.state.nodes["write"].provider_history
                )
            )
            selection = next(
                span
                for span in restarted_app.tracer.spans()
                if span.type is SpanType.PROVIDER_SELECT
            )
            self.assertEqual(selection.attributes["provider_id"], "restart-primary")
            self.assertEqual(selection.attributes["selector"], "provider-resume")
            selected_event = next(
                event
                for event in restarted_app.event_publisher.events()
                if event.name is ExecutionEventName.PROVIDER_SELECTED
            )
            self.assertEqual(selected_event.attributes["phase"], "resume")
            self.assertEqual(selected_event.attributes["provider_id"], "restart-primary")
