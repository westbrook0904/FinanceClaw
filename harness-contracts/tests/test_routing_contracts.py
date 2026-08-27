"""Stage 3B ExecutionMode 与 Route Contracts 的公共行为测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    ErrorCategory,
    ErrorCode,
    ExecutionMode,
    PlannerNotApplicableError,
    PlanningError,
    Request,
    RouteDecision,
    RouteSource,
    RouteType,
    RoutingError,
)
from pydantic import ValidationError


class ExecutionModeContractTests(unittest.TestCase):
    def test_old_request_json_defaults_to_auto(self) -> None:
        request = Request.model_validate(
            {
                "request_id": "req-old",
                "input": {"type": "text", "content": "compare providers"},
                "options": {"timeout_ms": 1_000, "trace": True},
            }
        )

        self.assertEqual(request.options.execution_mode, ExecutionMode.AUTO)
        self.assertEqual(request.model_dump(mode="json")["options"]["execution_mode"], "auto")

    def test_request_execution_mode_round_trips(self) -> None:
        payload = {
            "request_id": "req-plan",
            "input": {"type": "goal", "content": "compare providers"},
            "options": {"execution_mode": "plan"},
        }

        request = Request.model_validate(payload)
        restored = Request.model_validate(request.model_dump(mode="json"))

        self.assertEqual(request.options.execution_mode, ExecutionMode.PLAN)
        self.assertEqual(restored, request)


class RouteDecisionContractTests(unittest.TestCase):
    def test_valid_decisions_round_trip(self) -> None:
        decisions = (
            RouteDecision(
                mode=ExecutionMode.FAST,
                route_type=RouteType.DIRECT_CAPABILITY,
                source=RouteSource.RULE,
                capability_id="finance.query/v1",
                confidence=1.0,
                reason_code="EXPLICIT_TARGET",
                metadata={"catalog_hash": "abc123"},
            ),
            RouteDecision(
                mode=ExecutionMode.PLAN,
                route_type=RouteType.GENERATED_PLAN,
                source=RouteSource.MODEL,
                planner_id="llm-planner",
                confidence=0.75,
                reason_code="MULTI_STEP_GOAL",
            ),
            RouteDecision(
                mode=ExecutionMode.EXPLORE,
                route_type=RouteType.EXPLORATION,
                source=RouteSource.POLICY,
                explorer_id="bounded-explorer",
                reason_code="EXPLORATION_REQUIRED",
            ),
            RouteDecision(
                mode=ExecutionMode.HYBRID,
                route_type=RouteType.HYBRID,
                source=RouteSource.REQUEST,
                planner_id="hybrid-planner",
                explorer_id="bounded-explorer",
                reason_code="HYBRID_REQUESTED",
            ),
        )

        for decision in decisions:
            with self.subTest(mode=decision.mode):
                payload = decision.model_dump(mode="json")
                self.assertEqual(RouteDecision.model_validate(payload), decision)

    def test_auto_cannot_be_a_final_decision(self) -> None:
        with self.assertRaises(ValidationError):
            RouteDecision(
                mode=ExecutionMode.AUTO,
                route_type=RouteType.DIRECT_CAPABILITY,
                source=RouteSource.RULE,
                capability_id="finance.query/v1",
                reason_code="UNRESOLVED",
            )

    def test_mode_requires_matching_route_type(self) -> None:
        with self.assertRaises(ValidationError):
            RouteDecision(
                mode=ExecutionMode.FAST,
                route_type=RouteType.GENERATED_PLAN,
                source=RouteSource.RULE,
                capability_id="finance.query/v1",
                reason_code="INVALID_PAIR",
            )

    def test_route_target_fields_are_required_and_mutually_exclusive(self) -> None:
        invalid_payloads = (
            {
                "mode": "fast",
                "route_type": "direct_capability",
                "source": "rule",
                "reason_code": "MISSING_CAPABILITY",
            },
            {
                "mode": "plan",
                "route_type": "generated_plan",
                "source": "model",
                "planner_id": "llm-planner",
                "capability_id": "finance.query/v1",
                "reason_code": "CONFLICTING_TARGETS",
            },
            {
                "mode": "hybrid",
                "route_type": "hybrid",
                "source": "request",
                "planner_id": "hybrid-planner",
                "reason_code": "MISSING_EXPLORER",
            },
        )

        for payload in invalid_payloads:
            with self.subTest(reason_code=payload["reason_code"]):
                with self.assertRaises(ValidationError):
                    RouteDecision.model_validate(payload)

    def test_confidence_range_and_unknown_fields_are_rejected(self) -> None:
        valid_payload = {
            "mode": "fast",
            "route_type": "direct_capability",
            "source": "rule",
            "capability_id": "finance.query/v1",
            "reason_code": "EXPLICIT_TARGET",
        }

        for confidence in (-0.01, 1.01):
            with self.subTest(confidence=confidence):
                with self.assertRaises(ValidationError):
                    RouteDecision.model_validate({**valid_payload, "confidence": confidence})

        with self.assertRaises(ValidationError):
            RouteDecision.model_validate({**valid_payload, "provider_id": "provider-a"})


class RoutingAndPlanningErrorContractTests(unittest.TestCase):
    def test_error_codes_match_stage_three_b_contract(self) -> None:
        expected_codes = {
            ErrorCode.REQUEST_MODE_CONFLICT: "HARNESS.REQUEST.MODE_CONFLICT",
            ErrorCode.ROUTE_NO_MATCH: "HARNESS.ROUTE.NO_MATCH",
            ErrorCode.ROUTE_INVALID_DECISION: "HARNESS.ROUTE.INVALID_DECISION",
            ErrorCode.ROUTE_MODE_NOT_ALLOWED: "HARNESS.ROUTE.MODE_NOT_ALLOWED",
            ErrorCode.ROUTE_MODE_NOT_AVAILABLE: "HARNESS.ROUTE.MODE_NOT_AVAILABLE",
            ErrorCode.ROUTE_CAPABILITY_NOT_ALLOWED: "HARNESS.ROUTE.CAPABILITY_NOT_ALLOWED",
            ErrorCode.ROUTE_PLANNER_NOT_ALLOWED: "HARNESS.ROUTE.PLANNER_NOT_ALLOWED",
            ErrorCode.ROUTE_MODEL_FAILED: "HARNESS.ROUTE.MODEL_FAILED",
            ErrorCode.ROUTE_APPROVAL_NOT_SUPPORTED: "HARNESS.ROUTE.APPROVAL_NOT_SUPPORTED",
            ErrorCode.PLANNER_NOT_CONFIGURED: "HARNESS.PLANNER.NOT_CONFIGURED",
            ErrorCode.PLANNER_NOT_APPLICABLE: "HARNESS.PLANNER.NOT_APPLICABLE",
            ErrorCode.PLANNER_INVALID_OUTPUT: "HARNESS.PLANNER.INVALID_OUTPUT",
            ErrorCode.PLANNER_PLAN_TOO_LARGE: "HARNESS.PLANNER.PLAN_TOO_LARGE",
            ErrorCode.PLANNER_REPAIR_EXHAUSTED: "HARNESS.PLANNER.REPAIR_EXHAUSTED",
            ErrorCode.PLANNER_DEADLINE_EXCEEDED: "HARNESS.PLANNER.DEADLINE_EXCEEDED",
            ErrorCode.PLANNER_MODEL_FAILED: "HARNESS.PLANNER.MODEL_FAILED",
        }

        for code, value in expected_codes.items():
            with self.subTest(code=code):
                self.assertEqual(code.value, value)

    def test_routing_and_planning_errors_use_distinct_categories(self) -> None:
        routing = RoutingError("invalid route").to_detail()
        planning = PlanningError("invalid output").to_detail()
        not_applicable = PlannerNotApplicableError("template did not match").to_detail()

        self.assertEqual(routing.category, ErrorCategory.ROUTE)
        self.assertEqual(routing.code, ErrorCode.ROUTE_INVALID_DECISION)
        self.assertEqual(planning.category, ErrorCategory.PLANNER)
        self.assertEqual(planning.code, ErrorCode.PLANNER_INVALID_OUTPUT)
        self.assertEqual(not_applicable.category, ErrorCategory.PLANNER)
        self.assertEqual(not_applicable.code, ErrorCode.PLANNER_NOT_APPLICABLE)


if __name__ == "__main__":
    unittest.main()
