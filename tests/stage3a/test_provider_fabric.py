"""Provider Fabric 的 Retry、Fallback、WRITE safety 与 Health 验收。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityExecutionProfile,
    IdempotencyType,
    InvocationContext,
    ProviderHealthStatus,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
)
from harness_events import ExecutionEventName
from harness_selection import EligibilityPipeline, PrioritySelector, StaticHealthSource

from tests.stage3a.support import (
    AcceptanceProviderTool,
    make_request,
    provider_failure,
    register_provider,
)


async def invoke_provider_fabric(
    app,
    capability_id: str,
    request_id: str,
    *,
    retry_policy: RetryPolicy | None = None,
    idempotency_key: str | None = None,
):
    request = make_request(request_id)
    return await app.invoker.invoke(
        capability_id,
        request.input,
        InvocationContext(request=request),
        retry_policy=retry_policy,
        idempotency_key=idempotency_key,
    )


class ReadFallbackAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_retry_then_fallback_is_explainable(self) -> None:
        capability_id = "acceptance.finance-query/v1"
        profile = CapabilityExecutionProfile(side_effect=SideEffectType.READ)
        primary = AcceptanceProviderTool(
            capability_id,
            "primary",
            profile=profile,
            outcomes=(provider_failure("TEST.PRIMARY_TRANSIENT", retryable=True),),
        )
        backup = AcceptanceProviderTool(capability_id, "backup", profile=profile)
        app = build_harness(entry_point_group=None)
        register_provider(app.registry, primary, provider_id="finance-primary", priority=100)
        register_provider(app.registry, backup, provider_id="finance-backup", priority=50)

        await app.start()
        try:
            result = await invoke_provider_fabric(
                app,
                capability_id,
                "read-fallback",
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    initial_backoff_ms=0,
                    max_backoff_ms=0,
                ),
            )
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "backup")
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 1)
        event_names = [event.name for event in app.event_publisher.events()]
        self.assertEqual(
            event_names,
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
        fallback = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        )
        self.assertEqual(fallback.attributes["source_provider_id"], "finance-primary")
        self.assertEqual(fallback.attributes["target_provider_id"], "finance-backup")
        self.assertEqual([item.id for item in app.capability_catalog.list()], [capability_id])

    async def test_health_rejects_unhealthy_high_priority_provider(self) -> None:
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
        app = build_harness(entry_point_group=None, provider_selector=selector)
        register_provider(app.registry, primary, provider_id="health-primary", priority=100)
        register_provider(app.registry, backup, provider_id="health-backup", priority=10)

        await app.start()
        try:
            result = await invoke_provider_fabric(app, capability_id, "health-selection")
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
    async def _execute_write(self, *, name: str, source_group: str, target_group: str):
        capability_id = f"acceptance.{name}/v1"
        profile = CapabilityExecutionProfile(
            side_effect=SideEffectType.WRITE,
            idempotency=IdempotencyType.REQUIRED,
        )
        primary = AcceptanceProviderTool(
            capability_id,
            "primary",
            profile=profile,
            outcomes=(provider_failure("TEST.WRITE_PRIMARY"),),
        )
        backup = AcceptanceProviderTool(capability_id, "backup", profile=profile)
        app = build_harness(entry_point_group=None)
        register_provider(
            app.registry,
            primary,
            provider_id=f"{name}-primary",
            priority=100,
            equivalence_group=source_group,
        )
        register_provider(
            app.registry,
            backup,
            provider_id=f"{name}-backup",
            priority=50,
            equivalence_group=target_group,
        )
        await app.start()
        try:
            result = await invoke_provider_fabric(
                app,
                capability_id,
                f"{name}-request",
                idempotency_key=f"{name}-idempotency-key",
            )
        finally:
            await app.shutdown()
        return app, primary, backup, result

    async def test_idempotent_write_can_fallback_to_equivalent_provider(self) -> None:
        app, primary, backup, result = await self._execute_write(
            name="safe-write",
            source_group="payment-prod",
            target_group="payment-prod",
        )
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)
        self.assertTrue(
            any(
                event.name is ExecutionEventName.PROVIDER_FALLBACK
                for event in app.event_publisher.events()
            )
        )

    async def test_write_fallback_to_different_group_fails_closed(self) -> None:
        app, primary, backup, result = await self._execute_write(
            name="unsafe-write",
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


if __name__ == "__main__":
    unittest.main()
