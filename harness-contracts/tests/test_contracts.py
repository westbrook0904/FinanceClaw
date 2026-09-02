"""FinanceClaw 核心公共协议测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    ApprovalGrant,
    CapabilityDescriptor,
    CapabilityType,
    Continuation,
    PolicyError,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultIssue,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
)
from pydantic import ValidationError


class RequestAndCapabilityContractTests(unittest.TestCase):
    def test_request_round_trips_without_framework_execution_mode(self) -> None:
        request = Request(
            request_id="request-1",
            input=RequestInput(type="text", content="hello"),
            target=RequestTarget(capability="echo.reply/v1"),
            options=RequestOptions(timeout_ms=1000),
        )

        payload = request.model_dump(mode="json")
        restored = Request.model_validate(payload)

        self.assertEqual(restored, request)
        self.assertNotIn("execution_mode", payload["options"])

    def test_targetless_request_is_valid_for_future_agent_entry(self) -> None:
        request = Request(input=RequestInput(type="text", content="analyze portfolio"))
        self.assertIsNone(request.target)

    def test_capability_type_is_only_agent_or_tool(self) -> None:
        descriptor = CapabilityDescriptor(
            id="finance.query/v1",
            name="Finance Query",
            type=CapabilityType.AGENT,
            version="1.0.0",
        )
        self.assertEqual(descriptor.type, CapabilityType.AGENT)
        with self.assertRaises(ValidationError):
            CapabilityDescriptor(
                id="model/v1",
                name="Model",
                type="model",
                version="1.0.0",
            )


class ProviderRetryContractTests(unittest.TestCase):
    def test_retry_policy_round_trips(self) -> None:
        policy = RetryPolicy(
            max_attempts=3,
            initial_backoff_ms=10,
            max_backoff_ms=50,
            multiplier=2,
        )
        self.assertEqual(RetryPolicy.model_validate(policy.model_dump(mode="json")), policy)

    def test_retry_policy_rejects_invalid_backoff_range(self) -> None:
        with self.assertRaises(ValidationError):
            RetryPolicy(initial_backoff_ms=100, max_backoff_ms=10)


class ApprovalAndResultContractTests(unittest.TestCase):
    def test_approval_grant_remains_a_domain_policy_contract(self) -> None:
        grant = ApprovalGrant(
            approval_id="approval-1",
            plan_id="graph-run-1",
            node_id="tool-call-1",
            decided_by="reviewer",
            granted_at=datetime.now(UTC),
        )
        self.assertEqual(grant.approval_id, "approval-1")

    def test_success_and_failure_payload_rules(self) -> None:
        success = ResultEnvelope.success(ResultOutput(type="text", data="hello"))
        self.assertEqual(success.status, ResultStatus.SUCCESS)

        with self.assertRaises(ValidationError):
            ResultEnvelope(status=ResultStatus.FAILED)
        with self.assertRaises(ValidationError):
            ResultEnvelope(
                status=ResultStatus.FAILED,
                output=ResultOutput(type="text", data="unexpected"),
                error=PolicyError("denied").to_detail(),
            )

    def test_partial_accepted_and_cancelled_remain_transport_neutral(self) -> None:
        error = PolicyError("branch failed").to_detail()
        partial = ResultEnvelope.partial(
            ResultOutput(type="json", data={"usable": True}),
            [ResultIssue(source="tool-call", error=error)],
        )
        accepted = ResultEnvelope.accepted(
            Continuation(job_ref="job-1", waiting_reason="external completion")
        )
        cancelled = ResultEnvelope.cancelled()

        self.assertEqual(partial.status, ResultStatus.PARTIAL)
        self.assertEqual(accepted.status, ResultStatus.ACCEPTED)
        self.assertEqual(cancelled.status, ResultStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
