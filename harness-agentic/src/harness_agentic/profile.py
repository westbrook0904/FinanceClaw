"""可信 ExplorationProfile 的 Catalog eligibility 与快照物化。"""

from __future__ import annotations

from harness_contracts import (
    CapabilityCompletionMode,
    CapabilityType,
    EgressType,
    ErrorCode,
    ExplorationBudget,
    ExplorationError,
    ExplorationProfile,
    ExplorationProfileSnapshot,
    SideEffectType,
)
from harness_registry import CapabilityCatalog

from .canonical import exploration_profile_hash


class ExplorationProfileMaterializer:
    def __init__(self, catalog: CapabilityCatalog) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must implement CapabilityCatalog")
        self._catalog = catalog

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    def materialize(
        self,
        profile: ExplorationProfile,
        *,
        budget: ExplorationBudget | None = None,
    ) -> ExplorationProfileSnapshot:
        if not isinstance(profile, ExplorationProfile):
            raise TypeError("profile must be ExplorationProfile")
        if budget is not None and not isinstance(budget, ExplorationBudget):
            raise TypeError("budget must be ExplorationBudget or None")
        effective_budget = budget or profile.default_budget
        self._require_tightened_budget(profile.default_budget, effective_budget)
        self._validate_model(profile.model_capability_id)
        for capability_id in sorted(profile.allowed_capability_ids):
            self._validate_action_capability(capability_id)

        provisional = ExplorationProfileSnapshot(
            profile_id=profile.profile_id,
            model_capability_id=profile.model_capability_id,
            allowed_capability_ids=profile.allowed_capability_ids,
            budget=effective_budget,
            prompt_version=profile.prompt_version,
            memory_required=profile.memory_required,
            profile_hash="0" * 64,
        )
        return provisional.model_copy(
            update={"profile_hash": exploration_profile_hash(provisional)}
        )

    def _validate_model(self, capability_id: str) -> None:
        descriptor = self._catalog.get(capability_id)
        if descriptor is None or descriptor.type is not CapabilityType.MODEL:
            raise ExplorationError(
                "exploration profile model capability is unavailable or not MODEL",
                code=ErrorCode.EXPLORATION_INVALID_PROFILE,
                details={"capability_id": capability_id},
            )

    def _validate_action_capability(self, capability_id: str) -> None:
        descriptor = self._catalog.get(capability_id)
        if descriptor is None:
            self._ineligible(capability_id, "capability_not_found")
        assert descriptor is not None
        if descriptor.type not in {CapabilityType.AGENT, CapabilityType.TOOL}:
            self._ineligible(capability_id, "capability_type")
        execution = descriptor.execution_profile
        if execution.side_effect not in {SideEffectType.NONE, SideEffectType.READ}:
            self._ineligible(capability_id, "side_effect")
        if execution.egress not in {EgressType.NONE, EgressType.INTERNAL}:
            self._ineligible(capability_id, "egress")
        if execution.completion_mode is not CapabilityCompletionMode.SYNC:
            self._ineligible(capability_id, "completion_mode")

    @staticmethod
    def _require_tightened_budget(
        default: ExplorationBudget,
        effective: ExplorationBudget,
    ) -> None:
        for field_name in ExplorationBudget.model_fields:
            if getattr(effective, field_name) > getattr(default, field_name):
                raise ExplorationError(
                    "exploration budget override can only tighten profile defaults",
                    code=ErrorCode.EXPLORATION_INVALID_PROFILE,
                    details={"field": field_name},
                )

    @staticmethod
    def _ineligible(capability_id: str, reason: str) -> None:
        raise ExplorationError(
            "capability is not eligible for minimal exploration",
            code=ErrorCode.EXPLORATION_CAPABILITY_INELIGIBLE,
            details={"capability_id": capability_id, "reason": reason},
        )
