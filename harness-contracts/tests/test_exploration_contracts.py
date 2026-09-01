"""Agent Foundation F4a Minimal Explore wire contracts。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    ActionExecutionState,
    ActionProposal,
    CallCapabilityDraft,
    CapabilityCompletionMode,
    CapabilityExecutionProfile,
    ExecutionPlan,
    ExplorationBudget,
    ExplorationNodeSpec,
    ExplorationProfile,
    ExplorationProfileSnapshot,
    ExplorationState,
    ExplorationStatus,
    ExplorationTurnDraft,
    ExplorationUsage,
    FinishDraft,
    InvocationContext,
    NodeExecutionState,
    NodeExecutionStatus,
    NodeOutputBinding,
    Observation,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
)
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_steps=4,
        max_model_calls=6,
        max_action_calls=3,
        max_repeated_actions=1,
        max_observations=3,
    )


def profile_snapshot(*, profile_hash: str = "a" * 64) -> ExplorationProfileSnapshot:
    return ExplorationProfileSnapshot(
        profile_id="foundation-default",
        model_capability_id="model.reason/v1",
        allowed_capability_ids={"data.read/v1"},
        budget=budget(),
        prompt_version="explore-v1",
        memory_required=False,
        profile_hash=profile_hash,
    )


def exploration_plan(snapshot: ExplorationProfileSnapshot) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-explore",
        nodes=(
            PlanNode(
                node_id="exploration",
                kind=PlanNodeKind.EXPLORATION,
                exploration=ExplorationNodeSpec(
                    exploration_id="exploration-001",
                    goal_bindings={"goal": RequestBinding(pointer="/input")},
                    profile=snapshot,
                ),
            ),
        ),
        outputs={"result": NodeOutputBinding(node_id="exploration", pointer="/output")},
    )


class ExplorationContractTests(unittest.TestCase):
    def test_completion_mode_is_backward_compatible_but_explicitly_serialized(self) -> None:
        legacy = CapabilityExecutionProfile()
        synchronous = CapabilityExecutionProfile(completion_mode=CapabilityCompletionMode.SYNC)

        self.assertIs(legacy.completion_mode, CapabilityCompletionMode.UNKNOWN)
        self.assertEqual(legacy.model_dump(mode="json")["completion_mode"], "unknown")
        restored_legacy = CapabilityExecutionProfile.model_validate(
            {"side_effect": "read", "egress": "internal", "idempotency": "none"}
        )
        self.assertIs(restored_legacy.completion_mode, CapabilityCompletionMode.UNKNOWN)
        self.assertEqual(
            CapabilityExecutionProfile.model_validate(synchronous.model_dump(mode="json")),
            synchronous,
        )

    def test_profile_requires_nonempty_scope_and_basic_count_limits(self) -> None:
        profile = ExplorationProfile(
            profile_id="foundation-default",
            model_capability_id="model.reason/v1",
            allowed_capability_ids={"data.read/v1"},
            default_budget=budget(),
            prompt_version="explore-v1",
        )

        self.assertEqual(
            ExplorationProfile.model_validate(profile.model_dump(mode="json")),
            profile,
        )
        with self.assertRaises(ValidationError):
            ExplorationProfile.model_validate(
                {**profile.model_dump(mode="json"), "allowed_capability_ids": []}
            )
        with self.assertRaises(ValidationError):
            ExplorationBudget(
                max_steps=0,
                max_model_calls=1,
                max_action_calls=0,
                max_repeated_actions=0,
                max_observations=0,
            )

    def test_turn_draft_is_discriminated_and_rejects_runtime_owned_fields(self) -> None:
        adapter = TypeAdapter(ExplorationTurnDraft)
        call_payload = {
            "kind": "call_capability",
            "capability_id": "data.read/v1",
            "input": {"type": "json", "content": {"symbol": "AAA"}},
            "reason_code": "need_fresh_data",
        }
        finish_payload = {
            "kind": "finish",
            "output": {"type": "json", "data": {"answer": 42}},
            "evidence_refs": ["observation-001"],
            "reason_code": "goal_satisfied",
        }

        self.assertIsInstance(adapter.validate_python(call_payload), CallCapabilityDraft)
        self.assertIsInstance(adapter.validate_python(finish_payload), FinishDraft)
        for forbidden in (
            "plan_id",
            "node_id",
            "exploration_id",
            "action_id",
            "status",
            "budget",
            "provider_id",
            "plugin_id",
            "idempotency_key",
            "patch",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValidationError):
                adapter.validate_python({**call_payload, forbidden: "injected"})

    def test_exploration_node_fields_are_mutually_exclusive(self) -> None:
        snapshot = profile_snapshot()
        spec = ExplorationNodeSpec(
            exploration_id="exploration-001",
            goal_bindings={"goal": RequestBinding(pointer="/input")},
            profile=snapshot,
        )
        node = PlanNode(
            node_id="exploration",
            kind=PlanNodeKind.EXPLORATION,
            exploration=spec,
        )

        self.assertIsNone(node.capability)
        self.assertEqual(node.retry_policy.max_attempts, 1)
        with self.assertRaises(ValidationError):
            PlanNode(
                node_id="exploration",
                kind=PlanNodeKind.EXPLORATION,
                capability="data.read/v1",
                exploration=spec,
            )
        with self.assertRaises(ValidationError):
            PlanNode(
                node_id="exploration",
                kind=PlanNodeKind.EXPLORATION,
                exploration=spec,
                retry_policy=RetryPolicy(max_attempts=2),
            )
        with self.assertRaises(ValidationError):
            PlanNode(
                node_id="ordinary",
                capability="data.read/v1",
                exploration=spec,
            )

    def test_action_observation_and_nested_state_round_trip(self) -> None:
        proposal = ActionProposal(
            action_id="action-001",
            exploration_id="exploration-001",
            step=1,
            capability_id="data.read/v1",
            input=RequestInput(type="json", content={"symbol": "AAA"}),
            proposal_hash="b" * 64,
            catalog_snapshot_hash="c" * 64,
            scope_hash="d" * 64,
            context_projection_hash="e" * 64,
            reason_code="need_fresh_data",
        )
        result = ResultEnvelope.success(ResultOutput(type="json", data={"price": 10}))
        observation = Observation(
            observation_id="observation-001",
            action_id=proposal.action_id,
            result_status=ResultStatus.SUCCESS,
            bounded_summary={"price": 10},
            evidence_refs=("result:action-001",),
            result_hash="f" * 64,
        )
        action = ActionExecutionState(
            action_id=proposal.action_id,
            status="succeeded",
            proposal=proposal,
            result=result,
            observation_id=observation.observation_id,
            started_at=NOW,
            completed_at=NOW,
        )

        self.assertEqual(
            ActionExecutionState.model_validate(action.model_dump(mode="json")),
            action,
        )
        self.assertEqual(
            Observation.model_validate(observation.model_dump(mode="json")),
            observation,
        )
        with self.assertRaises(ValidationError):
            ActionExecutionState(
                action_id="other-action",
                proposal=proposal,
            )

    def test_plan_record_round_trip_enforces_outer_inner_profile_identity(self) -> None:
        snapshot = profile_snapshot()
        plan = exploration_plan(snapshot)
        child = ExplorationState(
            exploration_id="exploration-001",
            plan_id=plan.plan_id,
            node_id="exploration",
            profile=snapshot,
            status=ExplorationStatus.RUNNING,
            usage=ExplorationUsage(),
            scope_hash="d" * 64,
            started_at=NOW,
            updated_at=NOW,
        )
        state = PlanExecutionState(
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            status=PlanExecutionStatus.RUNNING,
            nodes={
                "exploration": NodeExecutionState(
                    node_id="exploration",
                    status=NodeExecutionStatus.RUNNING,
                    started_at=NOW,
                )
            },
            explorations={"exploration": child},
            started_at=NOW,
            updated_at=NOW,
        )
        record = PlanExecutionRecord(
            plan_id=plan.plan_id,
            plan=plan,
            context=InvocationContext(
                request=Request(input=RequestInput(type="goal", content="research"))
            ),
            state=state,
        )

        self.assertEqual(
            PlanExecutionRecord.model_validate_json(record.model_dump_json()),
            record,
        )
        mismatched = child.model_copy(update={"profile": profile_snapshot(profile_hash="9" * 64)})
        invalid_state = state.model_copy(
            deep=True,
            update={"explorations": {"exploration": mismatched}},
        )
        with self.assertRaises(ValidationError):
            PlanExecutionRecord(
                plan_id=plan.plan_id,
                plan=plan,
                context=record.context,
                state=invalid_state,
            )


if __name__ == "__main__":
    unittest.main()
