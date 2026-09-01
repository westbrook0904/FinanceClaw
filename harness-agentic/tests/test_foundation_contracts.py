"""Foundation F4a Profile、wrapper 与 checkpoint guards。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_agentic import (
    ExplorationCheckpointValidator,
    ExplorationPlanFactory,
    ExplorationProfileMaterializer,
    action_fingerprint,
    action_proposal_hash,
    canonical_json,
    exploration_profile_hash,
    exploration_scope_hash,
    result_envelope_hash,
)
from harness_contracts import (
    ActionExecutionState,
    ActionProposal,
    CapabilityCompletionMode,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    ContextConsumer,
    ContextUseRecord,
    EgressType,
    ErrorCode,
    ExecutionPlan,
    ExplorationBudget,
    ExplorationError,
    ExplorationProfile,
    ExplorationState,
    ExplorationStatus,
    ExplorationUsage,
    InvocationContext,
    NodeExecutionState,
    NodeExecutionStatus,
    Observation,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNodeKind,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    SideEffectType,
)
from harness_planning import PlanValidationCode, PlanValidationError, PlanValidator
from harness_registry import CapabilityCatalog

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class StubCatalog(CapabilityCatalog):
    def __init__(self, descriptors: tuple[CapabilityDescriptor, ...]) -> None:
        self._descriptors = {descriptor.id: descriptor for descriptor in descriptors}

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._descriptors.get(capability_id)

    def list(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))


def descriptor(
    capability_id: str,
    capability_type: CapabilityType,
    *,
    side_effect: SideEffectType = SideEffectType.NONE,
    egress: EgressType = EgressType.NONE,
    completion_mode: CapabilityCompletionMode = CapabilityCompletionMode.UNKNOWN,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id,
        name=capability_id,
        type=capability_type,
        version="1.0.0",
        execution_profile=CapabilityExecutionProfile(
            side_effect=side_effect,
            egress=egress,
            completion_mode=completion_mode,
        ),
    )


def default_budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_steps=4,
        max_model_calls=6,
        max_action_calls=3,
        max_repeated_actions=1,
        max_observations=3,
    )


def default_profile(*, allowed: frozenset[str] | None = None) -> ExplorationProfile:
    return ExplorationProfile(
        profile_id="foundation-default",
        model_capability_id="model.reason/v1",
        allowed_capability_ids=allowed or frozenset({"data.read/v1"}),
        default_budget=default_budget(),
        prompt_version="explore-v1",
    )


def valid_catalog() -> StubCatalog:
    return StubCatalog(
        (
            descriptor("model.reason/v1", CapabilityType.MODEL),
            descriptor(
                "data.read/v1",
                CapabilityType.TOOL,
                side_effect=SideEffectType.READ,
                egress=EgressType.INTERNAL,
                completion_mode=CapabilityCompletionMode.SYNC,
            ),
        )
    )


def context_use() -> ContextUseRecord:
    return ContextUseRecord(
        use_id="context-use-001",
        consumer=ContextConsumer.EXPLORE,
        snapshot_id="snapshot-001",
        snapshot_hash="1" * 64,
        projection_hash="2" * 64,
        included_item_ids=("request-goal",),
        assembled_at=NOW,
    )


def make_record(*, terminal: bool = False) -> PlanExecutionRecord:
    materializer = ExplorationProfileMaterializer(valid_catalog())
    snapshot = materializer.materialize(default_profile())
    template = ExplorationPlanFactory(
        materializer,
        exploration_id_factory=lambda: "exploration-001",
    ).create_template(
        default_profile(),
        goal_bindings={"goal": RequestBinding(pointer="/input")},
    )
    plan = ExecutionPlan(
        plan_id="plan-explore",
        nodes=template.nodes,
        edges=template.edges,
        outputs=template.outputs,
    )
    result = (
        ResultEnvelope.success(ResultOutput(type="json", data={"answer": 42})) if terminal else None
    )
    child = ExplorationState(
        exploration_id="exploration-001",
        plan_id=plan.plan_id,
        node_id="exploration",
        profile=snapshot,
        status=ExplorationStatus.SUCCEEDED if terminal else ExplorationStatus.RUNNING,
        usage=ExplorationUsage(steps=1, model_calls=1) if terminal else ExplorationUsage(),
        scope_hash=exploration_scope_hash(snapshot),
        context_uses=[context_use()] if terminal else [],
        final_result=result,
        started_at=NOW,
        updated_at=NOW,
        completed_at=NOW if terminal else None,
    )
    outer = NodeExecutionState(
        node_id="exploration",
        status=(NodeExecutionStatus.SUCCEEDED if terminal else NodeExecutionStatus.RUNNING),
        started_at=NOW,
        completed_at=NOW if terminal else None,
        result=result,
    )
    state = PlanExecutionState(
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        status=(PlanExecutionStatus.SUCCEEDED if terminal else PlanExecutionStatus.RUNNING),
        nodes={"exploration": outer},
        explorations={"exploration": child},
        started_at=NOW,
        updated_at=NOW,
        completed_at=NOW if terminal else None,
    )
    return PlanExecutionRecord(
        plan_id=plan.plan_id,
        plan=plan,
        context=InvocationContext(
            request=Request(input=RequestInput(type="goal", content="research"))
        ),
        state=state,
    )


def make_action_record() -> PlanExecutionRecord:
    record = make_record()
    child = record.state.explorations["exploration"]
    use = context_use()
    proposal = ActionProposal(
        action_id="action-001",
        exploration_id=child.exploration_id,
        step=1,
        capability_id="data.read/v1",
        input=RequestInput(type="json", content={"symbol": "AAA"}),
        proposal_hash="0" * 64,
        catalog_snapshot_hash="3" * 64,
        scope_hash=child.scope_hash,
        context_projection_hash=use.projection_hash,
        reason_code="need_fresh_data",
    )
    proposal = proposal.model_copy(update={"proposal_hash": action_proposal_hash(proposal)})
    result = ResultEnvelope.success(ResultOutput(type="json", data={"price": 10}))
    observation = Observation(
        observation_id="observation-001",
        action_id=proposal.action_id,
        result_status=ResultStatus.SUCCESS,
        bounded_summary={"price": 10},
        evidence_refs=("result:action-001",),
        result_hash=result_envelope_hash(result),
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
    child = child.model_copy(
        deep=True,
        update={
            "usage": ExplorationUsage(steps=1, model_calls=1, action_calls=1),
            "context_uses": [use],
            "actions": [action],
            "observations": [observation],
        },
    )
    state = record.state.model_copy(
        deep=True,
        update={"explorations": {"exploration": child}},
    )
    return record.model_copy(update={"state": state})


class ExplorationProfileTests(unittest.TestCase):
    def test_materializer_freezes_eligible_scope_and_only_tightens_budget(self) -> None:
        materializer = ExplorationProfileMaterializer(valid_catalog())
        tightened = ExplorationBudget(
            max_steps=3,
            max_model_calls=4,
            max_action_calls=2,
            max_repeated_actions=0,
            max_observations=2,
        )

        snapshot = materializer.materialize(default_profile(), budget=tightened)

        self.assertEqual(snapshot.budget, tightened)
        self.assertEqual(snapshot.profile_hash, exploration_profile_hash(snapshot))
        with self.assertRaises(ExplorationError) as raised:
            materializer.materialize(
                default_profile(),
                budget=ExplorationBudget(
                    max_steps=5,
                    max_model_calls=6,
                    max_action_calls=3,
                    max_repeated_actions=1,
                    max_observations=3,
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.EXPLORATION_INVALID_PROFILE)

    def test_scope_rejects_unknown_async_write_external_and_model_actions(self) -> None:
        invalid_descriptors = (
            descriptor("model.reason/v1", CapabilityType.MODEL),
            descriptor("unknown/v1", CapabilityType.TOOL),
            descriptor(
                "async/v1",
                CapabilityType.TOOL,
                completion_mode=CapabilityCompletionMode.ASYNC,
            ),
            descriptor(
                "write/v1",
                CapabilityType.AGENT,
                side_effect=SideEffectType.WRITE,
                completion_mode=CapabilityCompletionMode.SYNC,
            ),
            descriptor(
                "external/v1",
                CapabilityType.TOOL,
                egress=EgressType.EXTERNAL,
                completion_mode=CapabilityCompletionMode.SYNC,
            ),
        )
        catalog = StubCatalog(invalid_descriptors)
        materializer = ExplorationProfileMaterializer(catalog)

        for capability_id in (
            "unknown/v1",
            "async/v1",
            "write/v1",
            "external/v1",
            "model.reason/v1",
            "missing/v1",
        ):
            with self.subTest(capability_id=capability_id):
                with self.assertRaises(ExplorationError) as raised:
                    materializer.materialize(default_profile(allowed=frozenset({capability_id})))
                self.assertEqual(
                    raised.exception.code,
                    ErrorCode.EXPLORATION_CAPABILITY_INELIGIBLE,
                )


class ExplorationWrapperTests(unittest.TestCase):
    def test_factory_creates_only_the_structural_wrapper_and_execution_stays_closed(self) -> None:
        materializer = ExplorationProfileMaterializer(valid_catalog())
        template = ExplorationPlanFactory(
            materializer,
            exploration_id_factory=lambda: "exploration-001",
        ).create_template(
            default_profile(),
            goal_bindings={"goal": RequestBinding(pointer="/input")},
        )

        self.assertEqual(len(template.nodes), 1)
        self.assertIs(template.nodes[0].kind, PlanNodeKind.EXPLORATION)
        self.assertEqual(template.edges, ())
        self.assertEqual(tuple(template.outputs), ("result",))
        self.assertEqual(template.outputs["result"].pointer, "/output")
        self.assertEqual(template.nodes[0].metadata, {})
        PlanValidator(valid_catalog()).validate_template(template, executable=False)
        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator(valid_catalog()).validate_template(template)
        self.assertIn(
            PlanValidationCode.EXPLORATION_NOT_AVAILABLE,
            {issue.code for issue in raised.exception.issues},
        )

    def test_canonical_input_and_repeat_fingerprint_are_order_stable(self) -> None:
        first = RequestInput(type="json", content={"b": 2, "a": [1, 3]})
        second = RequestInput(type="json", content={"a": [1, 3], "b": 2})

        self.assertEqual(
            canonical_json(first.model_dump(mode="json")),
            canonical_json(second.model_dump(mode="json")),
        )
        self.assertEqual(
            action_fingerprint("data.read/v1", first),
            action_fingerprint("data.read/v1", second),
        )


class ExplorationCheckpointTests(unittest.TestCase):
    def test_active_and_terminal_snapshots_pass_wire_and_integrity_validation(self) -> None:
        validator = ExplorationCheckpointValidator()

        for record in (make_record(), make_record(terminal=True)):
            with self.subTest(status=record.state.status):
                restored = PlanExecutionRecord.model_validate_json(record.model_dump_json())
                self.assertEqual(validator.validate(restored), restored)

    def test_action_proposal_and_observation_hashes_are_revalidated(self) -> None:
        validator = ExplorationCheckpointValidator()
        record = make_action_record()

        self.assertEqual(validator.validate(record), record)
        child = record.state.explorations["exploration"]
        action = child.actions[0]
        observation = child.observations[0]
        corruptions = {
            "proposal": {
                "actions": [
                    action.model_copy(
                        update={
                            "proposal": action.proposal.model_copy(
                                update={"proposal_hash": "f" * 64}
                            )
                        }
                    )
                ]
            },
            "result": {"observations": [observation.model_copy(update={"result_hash": "f" * 64})]},
        }
        for name, update in corruptions.items():
            with self.subTest(name=name):
                corrupt_child = child.model_copy(update=update)
                corrupt_state = record.state.model_copy(
                    update={"explorations": {"exploration": corrupt_child}},
                )
                corrupt_record = record.model_copy(update={"state": corrupt_state})
                with self.assertRaises(ExplorationError) as raised:
                    validator.validate(corrupt_record)
                self.assertEqual(
                    raised.exception.code,
                    ErrorCode.EXPLORATION_CHECKPOINT_CORRUPT,
                )

    def test_corrupt_scope_and_outer_inner_state_fail_with_stable_error(self) -> None:
        validator = ExplorationCheckpointValidator()
        record = make_record()
        child = record.state.explorations["exploration"]
        wrong_scope = child.model_copy(update={"scope_hash": "f" * 64})
        wrong_scope_state = record.state.model_copy(
            deep=True,
            update={"explorations": {"exploration": wrong_scope}},
        )
        wrong_scope_record = record.model_copy(update={"state": wrong_scope_state})

        with self.assertRaises(ExplorationError) as scope_error:
            validator.validate(wrong_scope_record)
        self.assertEqual(
            scope_error.exception.code,
            ErrorCode.EXPLORATION_CHECKPOINT_CORRUPT,
        )

        invalid_outer = record.state.nodes["exploration"].model_copy(
            update={"status": NodeExecutionStatus.SUCCEEDED}
        )
        invalid_outer_state = record.state.model_copy(
            deep=True,
            update={"nodes": {"exploration": invalid_outer}},
        )
        invalid_outer_record = record.model_copy(update={"state": invalid_outer_state})
        with self.assertRaises(ExplorationError) as outer_error:
            validator.validate(invalid_outer_record)
        self.assertEqual(
            outer_error.exception.code,
            ErrorCode.EXPLORATION_CHECKPOINT_CORRUPT,
        )


if __name__ == "__main__":
    unittest.main()
