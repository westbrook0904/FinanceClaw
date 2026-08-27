"""Stage 3A Provider Contracts 的公共行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from harness_contracts import (
    EgressType,
    ErrorCode,
    PolicyError,
    ProviderAttempt,
    ProviderAttemptStatus,
    ProviderDescriptor,
    ProviderError,
    ProviderHealthSnapshot,
    ProviderHealthStatus,
    ProviderPin,
    SelectionContext,
    SelectionDecision,
    SelectionError,
    SelectionRejection,
    SideEffectType,
)


class ProviderContractTests(unittest.TestCase):
    def test_provider_descriptor_round_trips_and_is_frozen(self) -> None:
        descriptor = ProviderDescriptor(
            provider_id="finance-query-primary",
            capability_id="finance.query/v1",
            plugin_id="finance-query-plugin",
            implementation_version="3.0.0",
            priority=100,
            tags={"primary", "local"},
            region="sg",
            tenant_visibility={"tenant-a", "tenant-b"},
            equivalence_group="finance-query-prod",
            metadata={"deployment": {"zone": "a"}},
        )

        payload = descriptor.model_dump(mode="json")
        restored = ProviderDescriptor.model_validate(payload)

        self.assertEqual(restored, descriptor)
        self.assertEqual(payload["provider_id"], "finance-query-primary")
        self.assertNotIn("execution_profile", payload)
        with self.assertRaises(ValidationError):
            descriptor.priority = 50  # type: ignore[misc]

    def test_health_snapshot_requires_timezone(self) -> None:
        snapshot = ProviderHealthSnapshot(
            provider_id="finance-query-primary",
            status=ProviderHealthStatus.HEALTHY,
            observed_at=datetime.now(UTC),
            source="static",
        )

        self.assertEqual(snapshot.model_dump(mode="json")["status"], "healthy")
        with self.assertRaises(ValidationError):
            ProviderHealthSnapshot(
                provider_id="finance-query-primary",
                status=ProviderHealthStatus.UNKNOWN,
                observed_at=datetime.now(),
                source="test",
            )

    def test_provider_attempt_round_trips_and_validates_time_order(self) -> None:
        started = datetime.now(UTC)
        attempt = ProviderAttempt(
            provider_id="finance-query-primary",
            selection_key="selection-001",
            provider_attempt=1,
            retry_attempt=2,
            equivalence_group="finance-query-prod",
            started_at=started,
            completed_at=started + timedelta(milliseconds=10),
            status=ProviderAttemptStatus.SUCCEEDED,
        )

        self.assertEqual(
            ProviderAttempt.model_validate(attempt.model_dump(mode="json")),
            attempt,
        )
        with self.assertRaises(ValidationError):
            ProviderAttempt(
                provider_id="finance-query-primary",
                selection_key="selection-001",
                provider_attempt=1,
                retry_attempt=1,
                started_at=started,
                completed_at=started - timedelta(milliseconds=1),
                status=ProviderAttemptStatus.FAILED,
                failure_code="PROVIDER.TIMEOUT",
            )


class SelectionContractTests(unittest.TestCase):
    def test_selection_context_is_json_safe_and_frozen(self) -> None:
        context = SelectionContext(
            request_id="req-001",
            capability_id="finance.query/v1",
            tenant_id="tenant-a",
            identity_subject="user-a",
            side_effect=SideEffectType.READ,
            egress=EgressType.EXTERNAL,
            deadline_at=datetime.now(UTC) + timedelta(seconds=1),
            provider_pin=ProviderPin(
                provider_id="finance-query-primary",
                reason="replay",
            ),
            canary_subject="tenant-a:user-a",
            policy_constraints={"allowed_regions": ["sg"]},
        )

        restored = SelectionContext.model_validate(context.model_dump(mode="json"))

        self.assertEqual(restored, context)
        with self.assertRaises(TypeError):
            context.policy_constraints["allowed_regions"] = []  # type: ignore[index]

    def test_selection_decision_requires_consistent_candidate_sets(self) -> None:
        decision = SelectionDecision(
            capability_id="finance.query/v1",
            selected_provider_id="provider-a",
            eligible_candidates=("provider-a", "provider-b"),
            rejected_candidates=(
                SelectionRejection(
                    provider_id="provider-c",
                    reason_code="UNHEALTHY",
                ),
            ),
            selector="priority",
            reason_code="HIGHEST_PRIORITY",
            selection_key="selection-001",
        )

        restored = SelectionDecision.model_validate(decision.model_dump(mode="json"))
        self.assertEqual(restored, decision)

        with self.assertRaises(ValidationError):
            SelectionDecision(
                capability_id="finance.query/v1",
                selected_provider_id="provider-c",
                eligible_candidates=("provider-a", "provider-b"),
                selector="priority",
                reason_code="HIGHEST_PRIORITY",
                selection_key="selection-002",
            )

        with self.assertRaises(ValidationError):
            SelectionDecision(
                capability_id="finance.query/v1",
                selected_provider_id="provider-a",
                eligible_candidates=("provider-a", "provider-a"),
                selector="priority",
                reason_code="HIGHEST_PRIORITY",
                selection_key="selection-003",
            )


class ProviderErrorContractTests(unittest.TestCase):
    def test_error_detail_distinguishes_retry_and_fallback(self) -> None:
        detail = ProviderError(
            "provider unavailable",
            code=ErrorCode.PROVIDER_EXECUTION_FAILED,
            retryable=True,
            fallbackable=True,
            details={"provider_id": "provider-a"},
        ).to_detail()

        payload = detail.model_dump(mode="json")
        self.assertTrue(payload["retryable"])
        self.assertTrue(payload["fallbackable"])
        self.assertEqual(payload["category"], "provider")

    def test_existing_errors_remain_non_fallbackable_by_default(self) -> None:
        detail = PolicyError("denied").to_detail()

        self.assertFalse(detail.retryable)
        self.assertFalse(detail.fallbackable)

    def test_selection_error_has_selection_category(self) -> None:
        detail = SelectionError(
            "invalid decision",
            code=ErrorCode.SELECTION_INVALID_DECISION,
        ).to_detail()

        self.assertEqual(detail.category.value, "selection")
        self.assertFalse(detail.fallbackable)


if __name__ == "__main__":
    unittest.main()
