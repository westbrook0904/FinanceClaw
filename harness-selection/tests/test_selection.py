"""Stage 3A Eligibility / Health / PrioritySelector 行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    EgressType,
    ErrorCode,
    ProviderDescriptor,
    ProviderError,
    ProviderHealthStatus,
    ProviderPin,
    SelectionContext,
    SelectionError,
    SideEffectType,
)
from harness_registry import ProviderRegistration
from harness_selection import (
    EligibilityPipeline,
    EligibilityRejectionCode,
    PrioritySelector,
    StaticHealthSource,
    TestHealthSource,
)
from harness_selection.health import HealthSource
from harness_spi import Capability


class StubCapability(Capability):
    def __init__(self, capability_id: str = "web.search/v1") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor


def registration(
    provider_id: str,
    *,
    priority: int = 0,
    region: str | None = None,
    tenants: frozenset[str] = frozenset(),
    tags: frozenset[str] = frozenset(),
    capability_id: str = "web.search/v1",
) -> ProviderRegistration:
    provider = StubCapability(capability_id)
    return ProviderRegistration(
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=capability_id,
            plugin_id=f"{provider_id}-plugin",
            implementation_version="1.0.0",
            priority=priority,
            region=region,
            tenant_visibility=tenants,
            tags=tags,
        ),
        capability=provider.descriptor(),
        provider=provider,
    )


def context(**updates: object) -> SelectionContext:
    values: dict[str, object] = {
        "request_id": "req-001",
        "capability_id": "web.search/v1",
        "tenant_id": "tenant-a",
        "side_effect": SideEffectType.READ,
        "egress": EgressType.EXTERNAL,
    }
    values.update(updates)
    return SelectionContext(**values)


class BrokenHealthSource(HealthSource):
    def snapshot(self, provider_id: str):
        raise RuntimeError("health backend unavailable")


class HealthSourceTests(unittest.TestCase):
    def test_static_health_source_defaults_to_unknown(self) -> None:
        source = StaticHealthSource(
            {"google": ProviderHealthStatus.HEALTHY},
            observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        )

        self.assertEqual(source.snapshot("google").status, ProviderHealthStatus.HEALTHY)
        self.assertEqual(source.snapshot("baidu").status, ProviderHealthStatus.UNKNOWN)

    def test_test_health_source_can_change_status(self) -> None:
        source = TestHealthSource()
        source.set_status("google", ProviderHealthStatus.DEGRADED)
        self.assertEqual(source.snapshot("google").status, ProviderHealthStatus.DEGRADED)
        source.set_status("google", ProviderHealthStatus.HEALTHY)
        self.assertEqual(source.snapshot("google").status, ProviderHealthStatus.HEALTHY)


class EligibilityTests(unittest.TestCase):
    def test_filters_tenant_policy_region_health_and_pin(self) -> None:
        candidates = (
            registration("baidu", priority=90, region="cn", tenants=frozenset({"tenant-a"})),
            registration("bing", priority=80, region="us"),
            registration("google", priority=100, region="sg"),
            registration("sogou", priority=70, region="cn"),
        )
        health = TestHealthSource(
            {
                "baidu": ProviderHealthStatus.HEALTHY,
                "bing": ProviderHealthStatus.HEALTHY,
                "google": ProviderHealthStatus.UNHEALTHY,
                "sogou": ProviderHealthStatus.HEALTHY,
            }
        )
        pipeline = EligibilityPipeline(health)

        result = pipeline.evaluate(
            candidates,
            context(
                provider_pin=ProviderPin(provider_id="baidu", reason="test"),
                policy_constraints={"allowed_regions": ["cn", "sg"]},
            ),
        )

        self.assertEqual(tuple(item.provider_id for item in result.eligible), ("baidu",))
        reasons = {item.provider_id: item.reason_code for item in result.rejected}
        self.assertEqual(reasons["bing"], EligibilityRejectionCode.REGION_MISMATCH)
        self.assertEqual(reasons["google"], EligibilityRejectionCode.UNHEALTHY)
        self.assertEqual(reasons["sogou"], EligibilityRejectionCode.PIN_MISMATCH)

    def test_tenant_visibility_is_fail_closed(self) -> None:
        pipeline = EligibilityPipeline()
        result = pipeline.evaluate(
            [registration("tenant-only", tenants=frozenset({"tenant-b"}))],
            context(),
        )

        self.assertEqual(result.eligible, ())
        self.assertEqual(
            result.rejected[0].reason_code,
            EligibilityRejectionCode.TENANT_NOT_ALLOWED,
        )

    def test_unknown_policy_constraint_is_rejected(self) -> None:
        pipeline = EligibilityPipeline()

        with self.assertRaises(SelectionError) as raised:
            pipeline.evaluate(
                [registration("google")],
                context(policy_constraints={"future_unhandled_constraint": True}),
            )

        self.assertEqual(raised.exception.code, ErrorCode.SELECTION_INVALID_CONTEXT)

    def test_health_backend_failure_is_not_silently_ignored(self) -> None:
        pipeline = EligibilityPipeline(BrokenHealthSource())

        with self.assertRaises(ProviderError) as raised:
            pipeline.evaluate([registration("google")], context())

        self.assertEqual(raised.exception.code, ErrorCode.PROVIDER_HEALTH_UNAVAILABLE)


class PrioritySelectorTests(unittest.TestCase):
    def test_prefers_health_before_priority(self) -> None:
        candidates = (
            registration("degraded-high", priority=1000),
            registration("unknown-high", priority=500),
            registration("healthy-low", priority=10),
        )
        selector = PrioritySelector(
            EligibilityPipeline(
                StaticHealthSource(
                    {
                        "degraded-high": ProviderHealthStatus.DEGRADED,
                        "healthy-low": ProviderHealthStatus.HEALTHY,
                    }
                )
            )
        )

        decision = selector.select(candidates, context())

        self.assertEqual(decision.selected_provider_id, "healthy-low")
        self.assertEqual(
            decision.eligible_candidates,
            ("healthy-low", "unknown-high", "degraded-high"),
        )

    def test_priority_and_provider_id_are_deterministic_tiebreakers(self) -> None:
        candidates = (
            registration("provider-b", priority=100),
            registration("provider-a", priority=100),
            registration("provider-low", priority=50),
        )
        selector = PrioritySelector()

        first = selector.select(candidates, context())
        second = selector.select(tuple(reversed(candidates)), context())

        self.assertEqual(first.selected_provider_id, "provider-a")
        self.assertEqual(first.eligible_candidates, second.eligible_candidates)
        self.assertEqual(first.selection_key, second.selection_key)

    def test_pin_cannot_bypass_health_or_policy(self) -> None:
        candidate = registration("google", priority=100, region="sg")
        selector = PrioritySelector(
            EligibilityPipeline(StaticHealthSource({"google": ProviderHealthStatus.UNHEALTHY}))
        )

        with self.assertRaises(ProviderError) as unhealthy:
            selector.select(
                [candidate],
                context(provider_pin=ProviderPin(provider_id="google", reason="debug")),
            )
        self.assertEqual(unhealthy.exception.code, ErrorCode.PROVIDER_PIN_NOT_ALLOWED)

        healthy_selector = PrioritySelector()
        with self.assertRaises(ProviderError) as policy_denied:
            healthy_selector.select(
                [candidate],
                context(
                    provider_pin=ProviderPin(provider_id="google", reason="debug"),
                    policy_constraints={"denied_provider_ids": ["google"]},
                ),
            )
        self.assertEqual(policy_denied.exception.code, ErrorCode.PROVIDER_PIN_NOT_ALLOWED)

    def test_missing_pin_and_no_eligible_are_distinct_errors(self) -> None:
        selector = PrioritySelector()
        candidates = [registration("google")]

        with self.assertRaises(ProviderError) as missing_pin:
            selector.select(
                candidates,
                context(provider_pin=ProviderPin(provider_id="baidu", reason="debug")),
            )
        self.assertEqual(missing_pin.exception.code, ErrorCode.PROVIDER_PIN_NOT_FOUND)

        with self.assertRaises(ProviderError) as no_eligible:
            selector.select(
                candidates,
                context(policy_constraints={"denied_provider_ids": ["google"]}),
            )
        self.assertEqual(
            no_eligible.exception.code,
            ErrorCode.PROVIDER_NO_ELIGIBLE_CANDIDATE,
        )


if __name__ == "__main__":
    unittest.main()
