"""harness-contracts 的公共行为测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    ConditionExpr,
    ConditionOperator,
    Continuation,
    EdgeTrigger,
    EgressType,
    ExecutionPlan,
    ExecutionState,
    ExecutionStatus,
    FailurePolicy,
    IdempotencyType,
    IdentityContext,
    InvocationContext,
    LiteralBinding,
    NodeExecutionState,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanBudget,
    PlanEdge,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    PolicyError,
    ProviderAttempt,
    Request,
    RequestBinding,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultIssue,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SideEffectType,
    TraceContext,
    ValueReference,
)
from pydantic import ValidationError


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

    def test_plan_request_can_omit_target(self) -> None:
        request = Request(input=RequestInput(type="goal", content="compare providers"))

        self.assertIsNone(request.target)
        self.assertIsNone(request.model_dump(mode="json")["target"])


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

    def test_execution_profile_has_safe_defaults_and_round_trips(self) -> None:
        descriptor = CapabilityDescriptor(
            id="mail.send/v1",
            name="Send Mail",
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                egress=EgressType.EXTERNAL,
                idempotency=IdempotencyType.REQUIRED,
            ),
        )

        restored = CapabilityDescriptor.model_validate(descriptor.model_dump(mode="json"))
        self.assertEqual(restored, descriptor)
        self.assertEqual(restored.execution_profile.side_effect, SideEffectType.WRITE)


class PlanContractTests(unittest.TestCase):
    def make_plan(self) -> ExecutionPlan:
        condition = ConditionExpr(
            operator=ConditionOperator.AND,
            operands=(
                ConditionExpr(
                    operator=ConditionOperator.GTE,
                    ref=ValueReference(node_id="n1", pointer="/output/data/score"),
                    value=0.8,
                ),
                ConditionExpr(
                    operator=ConditionOperator.EXISTS,
                    ref=ValueReference(node_id="n1", pointer="/output/data/provider"),
                ),
            ),
        )
        return ExecutionPlan(
            plan_id="plan-001",
            revision=1,
            budget=PlanBudget(
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                max_concurrency=4,
            ),
            nodes=(
                PlanNode(
                    node_id="n1",
                    capability="finance.query/v1",
                    input_mapping={
                        "query": RequestBinding(pointer="/input/content"),
                        "limit": LiteralBinding(value=10),
                    },
                    retry_policy=RetryPolicy(
                        max_attempts=3,
                        initial_backoff_ms=10,
                        max_backoff_ms=100,
                    ),
                ),
                PlanNode(
                    node_id="n2",
                    capability="finance.rank/v1",
                    input_mapping={
                        "items": NodeOutputBinding(node_id="n1", pointer="/output/data/items")
                    },
                    failure_policy=FailurePolicy.CONTINUE,
                ),
            ),
            edges=(
                PlanEdge(
                    from_node="n1",
                    to_node="n2",
                    trigger=EdgeTrigger.SUCCESS,
                    condition=condition,
                ),
            ),
            outputs={"ranking": NodeOutputBinding(node_id="n2", pointer="/output/data")},
        )

    def test_plan_round_trip_is_json_safe_and_frozen(self) -> None:
        plan = self.make_plan()
        payload = plan.model_dump(mode="json")
        restored = ExecutionPlan.model_validate(payload)

        self.assertEqual(restored, plan)
        self.assertEqual(payload["nodes"][0]["input_mapping"]["query"]["kind"], "request")
        with self.assertRaises(TypeError):
            plan.nodes[0].input_mapping["other"] = LiteralBinding(value=1)  # type: ignore[index]
        with self.assertRaises(TypeError):
            plan.outputs["other"] = NodeOutputBinding(  # type: ignore[index]
                node_id="n1", pointer="/output/data"
            )

    def test_plan_rejects_invalid_structural_contracts(self) -> None:
        with self.assertRaises(ValidationError):
            PlanNode(node_id="n1", kind=PlanNodeKind.CAPABILITY)
        with self.assertRaises(ValidationError):
            PlanNode(
                node_id="approval",
                kind=PlanNodeKind.APPROVAL,
                capability="not-a-capability",
            )
        with self.assertRaises(ValidationError):
            PlanEdge(from_node="n1", to_node="n1")
        with self.assertRaises(ValidationError):
            RetryPolicy(max_attempts=2, initial_backoff_ms=100, max_backoff_ms=10)
        with self.assertRaises(ValidationError):
            ConditionExpr(operator=ConditionOperator.AND, operands=())
        with self.assertRaises(ValidationError):
            ExecutionPlan(
                plan_id="duplicate",
                nodes=(
                    PlanNode(node_id="n1", capability="a/v1"),
                    PlanNode(node_id="n1", capability="b/v1"),
                ),
            )


class ApprovalAndStateContractTests(unittest.TestCase):
    def test_approval_contracts_require_timezone_and_round_trip(self) -> None:
        approval = ApprovalRequest(
            approval_id="approval-001",
            plan_id="plan-001",
            node_id="n2",
            capability="mail.send/v1",
            resource_category="email",
            side_effect=SideEffectType.WRITE,
            egress=EgressType.EXTERNAL,
            parameter_summary={"recipient_count": 2},
            reason="External write requires approval",
        )
        decision = ApprovalDecision(
            approval_id=approval.approval_id,
            decision=ApprovalDecisionType.APPROVED,
            decided_by="operator-001",
        )

        self.assertEqual(ApprovalRequest.model_validate(approval.model_dump(mode="json")), approval)
        self.assertEqual(decision.decision, ApprovalDecisionType.APPROVED)
        with self.assertRaises(ValidationError):
            ApprovalDecision(
                approval_id="approval-001",
                decision=ApprovalDecisionType.REJECTED,
                decided_by="operator-001",
                decided_at=datetime.now(),
            )

    def test_plan_state_is_mutable_and_serializable(self) -> None:
        provider_attempt = ProviderAttempt(
            provider_id="provider-a",
            selection_key="selection-001",
            provider_attempt=1,
            retry_attempt=1,
            equivalence_group="finance-prod",
            started_at=datetime.now(UTC),
        )
        node = NodeExecutionState(
            node_id="n1",
            selected_provider_id="provider-a",
            provider_attempt=1,
            provider_retry_attempt=1,
            provider_selection_key="selection-001",
            provider_equivalence_group="finance-prod",
            provider_history=[provider_attempt],
        )
        state = PlanExecutionState(
            plan_id="plan-001",
            plan_revision=1,
            nodes={"n1": node},
        )
        node.status = NodeExecutionStatus.RUNNING
        state.status = PlanExecutionStatus.RUNNING
        state.state_version = 2

        restored = PlanExecutionState.model_validate(state.model_dump(mode="json"))
        self.assertEqual(restored.status, PlanExecutionStatus.RUNNING)
        self.assertEqual(restored.nodes["n1"].status, NodeExecutionStatus.RUNNING)
        self.assertEqual(restored.nodes["n1"].selected_provider_id, "provider-a")
        self.assertEqual(restored.nodes["n1"].provider_attempt, 1)
        self.assertEqual(restored.nodes["n1"].provider_retry_attempt, 1)
        self.assertEqual(restored.nodes["n1"].provider_history, [provider_attempt])
        self.assertEqual(restored.state_version, 2)

    def test_plan_execution_record_round_trips_and_checks_identity(self) -> None:
        request = Request(input=RequestInput(type="json", content={}))
        plan = ExecutionPlan(
            plan_id="record-plan",
            nodes=(PlanNode(node_id="n1", capability="record.work/v1"),),
        )
        state = PlanExecutionState(
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            nodes={"n1": NodeExecutionState(node_id="n1")},
        )
        record = PlanExecutionRecord(
            plan_id=plan.plan_id,
            plan=plan,
            context=InvocationContext(request=request),
            state=state,
        )

        restored = PlanExecutionRecord.model_validate_json(record.model_dump_json())

        self.assertEqual(restored, record)
        self.assertEqual(restored.state_version, state.state_version)
        with self.assertRaises(ValidationError):
            PlanExecutionRecord(
                plan_id="different-plan",
                plan=plan,
                context=InvocationContext(request=request),
                state=state,
            )


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

    def test_stage_two_result_statuses_enforce_payload_rules(self) -> None:
        error = PolicyError("local branch failed").to_detail()
        issue = ResultIssue(source="n2", error=error)
        partial = ResultEnvelope.partial(ResultOutput(type="json", data={"usable": True}), [issue])
        continuation = Continuation(plan_id="plan-001", node_id="n3", waiting_reason="approval")
        accepted = ResultEnvelope.accepted(continuation)
        cancelled = ResultEnvelope.cancelled()

        self.assertEqual(partial.status, ResultStatus.PARTIAL)
        self.assertEqual(accepted.status, ResultStatus.ACCEPTED)
        self.assertEqual(cancelled.status, ResultStatus.CANCELLED)
        with self.assertRaises(ValidationError):
            ResultEnvelope(status=ResultStatus.PARTIAL, output=ResultOutput(type="json", data={}))
        with self.assertRaises(ValidationError):
            ResultEnvelope(status=ResultStatus.ACCEPTED)
        with self.assertRaises(ValidationError):
            Continuation(waiting_reason="missing reference")


if __name__ == "__main__":
    unittest.main()
