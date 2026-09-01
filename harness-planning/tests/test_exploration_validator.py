"""PlanValidator 对 F4a Harness-owned Exploration node 的边界测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    ExecutionPlan,
    ExplorationBudget,
    ExplorationNodeSpec,
    ExplorationProfileSnapshot,
    FailurePolicy,
    NodeOutputBinding,
    PlanBudget,
    PlanNode,
    PlanNodeKind,
    RequestBinding,
    RetryPolicy,
)
from harness_planning import (
    PlanDraft,
    PlanNodeDraft,
    PlanValidationCode,
    PlanValidationError,
    PlanValidator,
)
from pydantic import ValidationError


def exploration_spec() -> ExplorationNodeSpec:
    return ExplorationNodeSpec(
        exploration_id="exploration-001",
        goal_bindings={"goal": RequestBinding(pointer="/input")},
        profile=ExplorationProfileSnapshot(
            profile_id="foundation-default",
            model_capability_id="model.reason/v1",
            allowed_capability_ids={"data.read/v1"},
            budget=ExplorationBudget(
                max_steps=4,
                max_model_calls=6,
                max_action_calls=3,
                max_repeated_actions=1,
                max_observations=3,
            ),
            prompt_version="explore-v1",
            memory_required=False,
            profile_hash="a" * 64,
        ),
    )


class ExplorationPlanValidatorTests(unittest.TestCase):
    def test_structural_validation_accepts_only_single_node_wrapper(self) -> None:
        exploration = PlanNode(
            node_id="exploration",
            kind=PlanNodeKind.EXPLORATION,
            exploration=exploration_spec(),
        )
        wrapper = ExecutionPlan(
            plan_id="plan-explore",
            nodes=(exploration,),
            outputs={"result": NodeOutputBinding(node_id="exploration", pointer="/output")},
        )

        self.assertIs(PlanValidator().validate(wrapper, executable=False), wrapper)

        invalid = ExecutionPlan(
            plan_id="plan-invalid-explore",
            nodes=(exploration, PlanNode(node_id="ordinary", capability="data.read/v1")),
            outputs={"result": NodeOutputBinding(node_id="exploration", pointer="/output")},
        )
        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(invalid, executable=False)
        self.assertIn(
            PlanValidationCode.INVALID_EXPLORATION_PLAN,
            {issue.code for issue in raised.exception.issues},
        )

    def test_default_execution_validation_remains_fail_closed(self) -> None:
        plan = ExecutionPlan(
            plan_id="plan-explore",
            nodes=(
                PlanNode(
                    node_id="exploration",
                    kind=PlanNodeKind.EXPLORATION,
                    exploration=exploration_spec(),
                ),
            ),
            outputs={"result": NodeOutputBinding(node_id="exploration", pointer="/output")},
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)
        self.assertIn(
            PlanValidationCode.EXPLORATION_NOT_AVAILABLE,
            {issue.code for issue in raised.exception.issues},
        )

    def test_validator_defensively_rejects_reverse_spec_injection(self) -> None:
        invalid = PlanNode.model_construct(
            node_id="ordinary",
            kind=PlanNodeKind.CAPABILITY,
            capability="data.read/v1",
            exploration=exploration_spec(),
            input_mapping={},
            timeout_ms=None,
            retry_policy=RetryPolicy(),
            failure_policy=FailurePolicy.FAIL_PLAN,
            idempotency_key=None,
            policy_tags=frozenset(),
            metadata={},
        )
        plan = ExecutionPlan.model_construct(
            plan_id="plan-invalid",
            revision=1,
            budget=PlanBudget(),
            failure_policy=FailurePolicy.FAIL_FAST,
            nodes=(invalid,),
            edges=(),
            outputs={},
            metadata={},
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan, executable=False)
        self.assertIn(
            PlanValidationCode.INVALID_CAPABILITY_NODE,
            {issue.code for issue in raised.exception.issues},
        )

    def test_model_plan_draft_cannot_name_exploration_kind_or_spec(self) -> None:
        kind_schema = PlanNodeDraft.model_json_schema()["properties"]["kind"]

        self.assertCountEqual(kind_schema["enum"], ["capability", "approval"])
        with self.assertRaises(ValidationError):
            PlanNodeDraft.model_validate(
                {
                    "node_id": "exploration",
                    "kind": "exploration",
                    "exploration": exploration_spec().model_dump(mode="json"),
                }
            )
        self.assertNotIn(
            "exploration",
            PlanDraft.model_json_schema()["$defs"]["PlanNodeDraft"]["properties"],
        )


if __name__ == "__main__":
    unittest.main()
