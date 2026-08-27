"""Stage 3A multi-provider、Fallback、WRITE safety 与 Health 验收。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityExecutionProfile,
    ExecutionPlan,
    IdempotencyType,
    NodeOutputBinding,
    PlanNode,
    ProviderHealthStatus,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
)
from harness_events import ExecutionEventName, InMemoryEventBus
from harness_selection import EligibilityPipeline, PrioritySelector, StaticHealthSource
from harness_trace import InMemoryTracer, SpanType

from tests.stage3a.support import (
    AcceptanceProviderTool,
    make_request,
    provider_failure,
    register_provider,
)


class ReadFallbackAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_retry_then_fallback_is_explainable_end_to_end(self) -> None:
        capability_id = "acceptance.finance-query/v1"
        profile = CapabilityExecutionProfile(side_effect=SideEffectType.READ)
        primary = AcceptanceProviderTool(
            capability_id,
            "primary",
            profile=profile,
            outcomes=(
                provider_failure(
                    "TEST.3A.PRIMARY_TRANSIENT",
                    retryable=True,
                ),
            ),
        )
        backup = AcceptanceProviderTool(capability_id, "backup", profile=profile)
        app = build_harness(entry_point_group=None)
        register_provider(
            app.registry,
            primary,
            provider_id="finance-primary",
            priority=100,
        )
        register_provider(
            app.registry,
            backup,
            provider_id="finance-backup",
            priority=50,
        )
        plan = ExecutionPlan(
            plan_id="stage3a-read-fallback",
            nodes=(
                PlanNode(
                    node_id="query",
                    capability=capability_id,
                    retry_policy=RetryPolicy(
                        max_attempts=2,
                        initial_backoff_ms=0,
                        max_backoff_ms=0,
                    ),
                ),
            ),
            outputs={
                "provider": NodeOutputBinding(
                    node_id="query",
                    pointer="/output/data/provider",
                )
            },
        )

        await app.start()
        try:
            result = await app.execute_plan(make_request("stage3a-read-request"), plan)
            saved = await app.state_store.load(plan.plan_id)
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "backup")
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 1)
        self.assertEqual(
            [attempt.provider_id for attempt in saved.state.nodes["query"].provider_history],
            ["finance-primary", "finance-primary", "finance-backup"],
        )
        self.assertEqual(saved.state.nodes["query"].selected_provider_id, "finance-backup")

        self.assertIsInstance(app.event_publisher, InMemoryEventBus)
        event_names = [event.name for event in app.event_publisher.events()]
        self.assertEqual(
            [
                name
                for name in event_names
                if name
                in {
                    ExecutionEventName.PROVIDER_CANDIDATES,
                    ExecutionEventName.PROVIDER_SELECTED,
                    ExecutionEventName.PROVIDER_FAILED,
                    ExecutionEventName.PROVIDER_RETRYING,
                    ExecutionEventName.PROVIDER_FALLBACK,
                }
            ],
            [
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_SELECTED,
                ExecutionEventName.PROVIDER_FAILED,
                ExecutionEventName.PROVIDER_RETRYING,
                ExecutionEventName.PROVIDER_FAILED,
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_FALLBACK,
                ExecutionEventName.PROVIDER_SELECTED,
            ],
        )
        for required in (
            ExecutionEventName.PLAN_CREATED,
            ExecutionEventName.PLAN_STARTED,
            ExecutionEventName.NODE_READY,
            ExecutionEventName.NODE_STARTED,
            ExecutionEventName.NODE_COMPLETED,
            ExecutionEventName.CHECKPOINT_SAVED,
            ExecutionEventName.PLAN_COMPLETED,
        ):
            self.assertIn(required, event_names)
        self.assertLess(
            event_names.index(ExecutionEventName.NODE_STARTED),
            event_names.index(ExecutionEventName.PROVIDER_CANDIDATES),
        )
        self.assertLess(
            event_names.index(ExecutionEventName.PROVIDER_SELECTED),
            event_names.index(ExecutionEventName.NODE_COMPLETED),
        )
        self.assertLess(
            event_names.index(ExecutionEventName.NODE_COMPLETED),
            event_names.index(ExecutionEventName.PLAN_COMPLETED),
        )
        fallback = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        )
        self.assertEqual(fallback.attributes["source_provider_id"], "finance-primary")
        self.assertEqual(fallback.attributes["target_provider_id"], "finance-backup")

        self.assertIsInstance(app.tracer, InMemoryTracer)
        selection_spans = [
            span for span in app.tracer.spans() if span.type is SpanType.PROVIDER_SELECT
        ]
        self.assertEqual(
            [span.attributes["provider_id"] for span in selection_spans],
            ["finance-primary", "finance-backup"],
        )
        trace_fallback = next(
            event for event in app.tracer.events() if event.name == "provider.fallback"
        )
        self.assertEqual(trace_fallback.attributes["provider_attempt"], 2)

        catalog = app.capability_catalog.list()
        self.assertEqual([descriptor.id for descriptor in catalog], [capability_id])

    async def test_minimal_health_rejects_unhealthy_high_priority_provider(self) -> None:
        capability_id = "acceptance.health-query/v1"
        primary = AcceptanceProviderTool(capability_id, "primary")
        backup = AcceptanceProviderTool(capability_id, "backup")
        selector = PrioritySelector(
            EligibilityPipeline(
                StaticHealthSource(
                    {
                        "health-primary": ProviderHealthStatus.UNHEALTHY,
                        "health-backup": ProviderHealthStatus.HEALTHY,
                    }
                )
            )
        )
        app = build_harness(
            entry_point_group=None,
            provider_selector=selector,
        )
        register_provider(
            app.registry,
            primary,
            provider_id="health-primary",
            priority=100,
        )
        register_provider(
            app.registry,
            backup,
            provider_id="health-backup",
            priority=10,
        )
        plan = ExecutionPlan(
            plan_id="stage3a-minimal-health",
            nodes=(PlanNode(node_id="query", capability=capability_id),),
        )

        await app.start()
        try:
            result = await app.execute_plan(make_request("stage3a-health-request"), plan)
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 0)
        self.assertEqual(backup.calls, 1)
        candidates = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.PROVIDER_CANDIDATES
        )
        self.assertEqual(
            candidates.model_dump(mode="json")["attributes"]["rejected_candidates"],
            [{"provider_id": "health-primary", "reason_code": "UNHEALTHY"}],
        )


class WriteFallbackAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def _execute_write(
        self,
        *,
        plan_id: str,
        source_group: str,
        target_group: str,
    ):
        capability_id = f"acceptance.{plan_id}/v1"
        profile = CapabilityExecutionProfile(
            side_effect=SideEffectType.WRITE,
            idempotency=IdempotencyType.REQUIRED,
        )
        primary = AcceptanceProviderTool(
            capability_id,
            "primary",
            profile=profile,
            outcomes=(provider_failure("TEST.3A.WRITE_PRIMARY"),),
        )
        backup = AcceptanceProviderTool(capability_id, "backup", profile=profile)
        app = build_harness(entry_point_group=None)
        register_provider(
            app.registry,
            primary,
            provider_id=f"{plan_id}-primary",
            priority=100,
            equivalence_group=source_group,
        )
        register_provider(
            app.registry,
            backup,
            provider_id=f"{plan_id}-backup",
            priority=50,
            equivalence_group=target_group,
        )
        plan = ExecutionPlan(
            plan_id=plan_id,
            nodes=(
                PlanNode(
                    node_id="write",
                    capability=capability_id,
                    idempotency_key=f"{plan_id}-idempotency-key",
                ),
            ),
        )

        await app.start()
        try:
            result = await app.execute_plan(make_request(f"{plan_id}-request"), plan)
        finally:
            await app.shutdown()
        return app, primary, backup, result

    async def test_idempotent_write_can_fallback_to_equivalent_provider(self) -> None:
        app, primary, backup, result = await self._execute_write(
            plan_id="stage3a-safe-write",
            source_group="payment-prod",
            target_group="payment-prod",
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)
        fallback = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        )
        self.assertEqual(
            fallback.attributes["target_provider_id"],
            "stage3a-safe-write-backup",
        )

    async def test_write_fallback_to_different_group_fails_closed(self) -> None:
        app, primary, backup, result = await self._execute_write(
            plan_id="stage3a-unsafe-write",
            source_group="payment-prod",
            target_group="payment-backup",
        )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.FALLBACK_UNSAFE")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 0)
        self.assertFalse(
            any(
                event.name is ExecutionEventName.PROVIDER_FALLBACK
                for event in app.event_publisher.events()
            )
        )
