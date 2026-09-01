"""F4a Exploration nested checkpoint 的确定性完整性校验。"""

from __future__ import annotations

from collections import Counter
from typing import NoReturn

from harness_contracts import (
    ErrorCode,
    ExplorationError,
    ExplorationStatus,
    PlanExecutionRecord,
    ResultStatus,
)
from pydantic import ValidationError

from .canonical import (
    action_fingerprint,
    action_proposal_hash,
    exploration_profile_hash,
    exploration_scope_hash,
    result_envelope_hash,
)


class ExplorationCheckpointValidator:
    def validate(self, record: PlanExecutionRecord) -> PlanExecutionRecord:
        if not isinstance(record, PlanExecutionRecord):
            raise TypeError("record must be PlanExecutionRecord")
        try:
            PlanExecutionRecord.model_validate(record.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError) as exc:
            self._corrupt("outer_inner_invariant", cause=exc)

        for exploration in record.state.explorations.values():
            self._validate_exploration(exploration)
        return record

    def _validate_exploration(self, exploration: object) -> None:
        from harness_contracts import ExplorationState

        if not isinstance(exploration, ExplorationState):
            self._corrupt("invalid_exploration_state")
        if exploration.profile.profile_hash != exploration_profile_hash(exploration.profile):
            self._corrupt("profile_hash_mismatch")
        if exploration.scope_hash != exploration_scope_hash(exploration.profile):
            self._corrupt("scope_hash_mismatch")

        budget = exploration.profile.budget
        usage = exploration.usage
        if (
            usage.steps > budget.max_steps
            or usage.model_calls > budget.max_model_calls
            or usage.action_calls > budget.max_action_calls
            or len(exploration.observations) > budget.max_observations
        ):
            self._corrupt("budget_exceeded")
        if usage.steps > usage.model_calls:
            self._corrupt("step_usage_exceeds_model_usage")
        if usage.action_calls != len(exploration.actions):
            self._corrupt("action_usage_mismatch")
        if usage.model_calls != len(exploration.context_uses):
            self._corrupt("model_usage_mismatch")

        projection_hashes = {
            context_use.projection_hash for context_use in exploration.context_uses
        }
        actions = {action.action_id: action for action in exploration.actions}
        observations = {
            observation.action_id: observation for observation in exploration.observations
        }
        if len(observations) != len(exploration.observations):
            self._corrupt("multiple_observations_for_action")
        if set(observations).difference(actions):
            self._corrupt("observation_action_not_found")

        fingerprints = Counter()
        steps: set[int] = set()
        for action in exploration.actions:
            proposal = action.proposal
            if proposal.exploration_id != exploration.exploration_id:
                self._corrupt("action_exploration_mismatch")
            if proposal.scope_hash != exploration.scope_hash:
                self._corrupt("action_scope_mismatch")
            if proposal.capability_id not in exploration.profile.allowed_capability_ids:
                self._corrupt("action_outside_scope")
            if proposal.context_projection_hash not in projection_hashes:
                self._corrupt("action_context_reference_missing")
            if proposal.proposal_hash != action_proposal_hash(proposal):
                self._corrupt("action_proposal_hash_mismatch")
            if proposal.step in steps or proposal.step > usage.steps:
                self._corrupt("action_step_invalid")
            steps.add(proposal.step)
            fingerprints[action_fingerprint(proposal.capability_id, proposal.input)] += 1
            self._validate_action(action, observations.get(action.action_id))

        if any(count - 1 > budget.max_repeated_actions for count in fingerprints.values()):
            self._corrupt("repeated_action_budget_exceeded")

        pending = exploration.pending_action_id
        if pending is None:
            if any(action.status in {"proposed", "running"} for action in actions.values()):
                self._corrupt("pending_action_missing")
        else:
            action = actions.get(pending)
            if action is None:
                self._corrupt("pending_action_not_found")
            assert action is not None
            if action.status not in {"proposed", "running"}:
                self._corrupt("pending_action_status_invalid")

        if (
            exploration.status
            in {
                ExplorationStatus.SUCCEEDED,
                ExplorationStatus.PARTIAL,
                ExplorationStatus.FAILED,
                ExplorationStatus.DENIED,
                ExplorationStatus.CANCELLED,
            }
            and exploration.pending_action_id is not None
        ):
            self._corrupt("terminal_exploration_has_pending_action")

    def _validate_action(self, action: object, observation: object | None) -> None:
        from harness_contracts import ActionExecutionState, Observation

        if not isinstance(action, ActionExecutionState):
            self._corrupt("invalid_action_state")
        if observation is not None and not isinstance(observation, Observation):
            self._corrupt("invalid_observation")
        if action.status in {"proposed", "running"}:
            if action.result is not None or action.observation_id is not None:
                self._corrupt("nonterminal_action_has_result")
            return
        if action.completed_at is None or action.result is None:
            self._corrupt("terminal_action_missing_result")
        assert action.result is not None

        expected_statuses = {
            "succeeded": {ResultStatus.SUCCESS, ResultStatus.PARTIAL},
            "failed": {ResultStatus.FAILED},
            "denied": {ResultStatus.DENIED},
            "cancelled": {ResultStatus.CANCELLED},
            "orphaned": {ResultStatus.ACCEPTED},
        }
        if action.status not in expected_statuses:
            self._corrupt("action_status_invalid")
        if action.result.status not in expected_statuses[action.status]:
            self._corrupt("action_result_status_mismatch")
        if action.status in {"succeeded", "failed"}:
            if observation is None or action.observation_id != observation.observation_id:
                self._corrupt("action_observation_missing")
            assert isinstance(observation, Observation)
            if observation.result_status is not action.result.status:
                self._corrupt("observation_status_mismatch")
            if observation.result_hash != result_envelope_hash(action.result):
                self._corrupt("observation_result_hash_mismatch")
        elif observation is not None or action.observation_id is not None:
            self._corrupt("governed_or_orphaned_action_has_observation")
        if action.status == "orphaned" and action.error_code is None:
            self._corrupt("orphaned_action_missing_error")

    @staticmethod
    def _corrupt(reason: str, *, cause: Exception | None = None) -> NoReturn:
        error = ExplorationError(
            "exploration checkpoint is corrupt",
            code=ErrorCode.EXPLORATION_CHECKPOINT_CORRUPT,
            details={"reason": reason},
        )
        if cause is None:
            raise error
        raise error from cause
