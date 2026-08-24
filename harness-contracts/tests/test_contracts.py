"""harness-contracts 的公共行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionState,
    ExecutionStatus,
    IdentityContext,
    InvocationContext,
    PolicyError,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    TraceContext,
)


def make_request() -> Request:
    return Request(
        request_id="req-001",
        session_id="session-001",
        tenant_id="tenant-a",
        user_id="user-a",
        input=RequestInput(type="text", content="hello"),
        target=RequestTarget(capability="echo.reply/v1"),
        options=RequestOptions(timeout_ms=1_000),
    )


class RequestContractTests(unittest.TestCase):
    def test_request_round_trip_is_json_safe(self) -> None:
        request = make_request()

        payload = request.model_dump(mode="json")
        restored = Request.model_validate(payload)

        self.assertEqual(restored, request)
        self.assertEqual(payload["target"]["capability"], "echo.reply/v1")

    def test_request_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            Request.model_validate(
                {
                    "input": {"type": "text", "content": "hello"},
                    "target": {"capability": "echo.reply/v1"},
                    "unknown": True,
                }
            )

    def test_timeout_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError):
            RequestOptions(timeout_ms=0)


class ContextContractTests(unittest.TestCase):
    def test_context_is_frozen_but_execution_state_is_mutable(self) -> None:
        context = InvocationContext(
            request=make_request(),
            identity=IdentityContext(subject="user-a", scopes={"echo.invoke"}),
            deadline_at=datetime.now(UTC) + timedelta(seconds=1),
            attributes={"nested": {"items": [1, 2]}},
            trace_context=TraceContext(trace_id="trace-001"),
        )

        with self.assertRaises(ValidationError):
            context.deadline_at = datetime.now(UTC)  # type: ignore[misc]

        with self.assertRaises(TypeError):
            context.attributes["new"] = True  # type: ignore[index]

        nested = context.attributes["nested"]
        with self.assertRaises(TypeError):
            nested["items"] = []  # type: ignore[index]

        state = ExecutionState()
        state.status = ExecutionStatus.RUNNING
        self.assertEqual(state.status, ExecutionStatus.RUNNING)

        payload = context.model_dump(mode="json")
        self.assertEqual(payload["attributes"]["nested"]["items"], [1, 2])

    def test_context_rejects_naive_deadline(self) -> None:
        with self.assertRaises(ValidationError):
            InvocationContext(request=make_request(), deadline_at=datetime.now())


class CapabilityContractTests(unittest.TestCase):
    def test_descriptor_serializes_enum_values(self) -> None:
        descriptor = CapabilityDescriptor(
            id="echo.reply/v1",
            name="Echo Reply",
            type=CapabilityType.AGENT,
            version="1.0.0",
            tags={"local", "example"},
        )
        payload = descriptor.model_dump(mode="json")

        self.assertEqual(payload["type"], "agent")
        self.assertCountEqual(payload["tags"], ["local", "example"])


class ResultAndErrorContractTests(unittest.TestCase):
    def test_success_factory_builds_valid_envelope(self) -> None:
        result = ResultEnvelope.success(
            ResultOutput(type="text", data="hello"),
            trace_id="trace-001",
            metadata={"provider": "echo-agent"},
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertIsNone(result.error)

    def test_failure_requires_error_and_forbids_output(self) -> None:
        with self.assertRaises(ValidationError):
            ResultEnvelope(status=ResultStatus.FAILED)

        with self.assertRaises(ValidationError):
            ResultEnvelope(
                status=ResultStatus.FAILED,
                output=ResultOutput(type="text", data="unexpected"),
                error=PolicyError("denied").to_detail(),
            )

    def test_harness_error_converts_to_safe_detail(self) -> None:
        detail = PolicyError("scope missing", details={"scope": "echo.invoke"}).to_detail()

        self.assertEqual(detail.code, "HARNESS.POLICY.DENIED")
        self.assertEqual(detail.category.value, "policy")
        self.assertFalse(detail.retryable)


if __name__ == "__main__":
    unittest.main()
