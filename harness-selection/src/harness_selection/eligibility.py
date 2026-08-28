"""Provider Eligibility 过滤与结构化拒绝原因。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Self

from harness_contracts import (
    ContractModel,
    ErrorCode,
    ProviderError,
    ProviderHealthSnapshot,
    ProviderHealthStatus,
    SelectionContext,
    SelectionError,
    SelectionRejection,
)
from harness_registry import ProviderRegistration
from pydantic import Field, model_validator

from .health import HealthSource, StaticHealthSource

NonEmptyString = Annotated[str, Field(min_length=1)]


class EligibilityRejectionCode(StrEnum):
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    PROVIDER_NOT_ALLOWED = "PROVIDER_NOT_ALLOWED"
    PROVIDER_DENIED = "PROVIDER_DENIED"
    REGION_MISMATCH = "REGION_MISMATCH"
    REQUIRED_TAG_MISSING = "REQUIRED_TAG_MISSING"
    TENANT_NOT_ALLOWED = "TENANT_NOT_ALLOWED"
    UNHEALTHY = "UNHEALTHY"
    PIN_MISMATCH = "PIN_MISMATCH"


class SelectionPolicyConstraints(ContractModel):
    """Stage 3A Selection 当前正式支持的 Policy constraints。"""

    allowed_provider_ids: frozenset[NonEmptyString] | None = None
    denied_provider_ids: frozenset[NonEmptyString] = Field(default_factory=frozenset)
    allowed_regions: frozenset[NonEmptyString] | None = None
    required_region: NonEmptyString | None = None
    required_provider_tags: frozenset[NonEmptyString] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def validate_region_constraints(self) -> Self:
        if (
            self.required_region is not None
            and self.allowed_regions is not None
            and self.required_region not in self.allowed_regions
        ):
            raise ValueError("required_region must be included in allowed_regions")
        return self


@dataclass(frozen=True, slots=True)
class EligibleProvider:
    registration: ProviderRegistration
    health: ProviderHealthSnapshot

    @property
    def provider_id(self) -> str:
        return self.registration.provider_id


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: tuple[EligibleProvider, ...]
    rejected: tuple[SelectionRejection, ...]


class EligibilityPipeline:
    """按固定顺序执行兼容性、Policy、Tenant、Health 和 Pin 过滤。"""

    def __init__(self, health_source: HealthSource | None = None) -> None:
        effective_health_source = health_source or StaticHealthSource()
        if not isinstance(effective_health_source, HealthSource):
            raise TypeError("health_source must implement HealthSource")
        self._health_source = effective_health_source

    @property
    def health_source(self) -> HealthSource:
        return self._health_source

    def evaluate(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
    ) -> EligibilityResult:
        if not isinstance(context, SelectionContext):
            raise TypeError("context must be SelectionContext")

        candidate_tuple = tuple(candidates)
        if any(not isinstance(item, ProviderRegistration) for item in candidate_tuple):
            raise TypeError("candidates must contain ProviderRegistration values")
        ordered = tuple(sorted(candidate_tuple, key=lambda item: item.provider_id))
        provider_ids = tuple(item.provider_id for item in ordered)
        if len(provider_ids) != len(set(provider_ids)):
            raise SelectionError(
                "selection candidates contain duplicate provider ids",
                code=ErrorCode.SELECTION_INVALID_CONTEXT,
                details={"provider_ids": list(provider_ids)},
            )

        constraints = self._parse_constraints(context)
        eligible: list[EligibleProvider] = []
        rejected: list[SelectionRejection] = []

        for registration in ordered:
            rejection = self._evaluate_candidate(registration, context, constraints)
            if rejection is not None:
                rejected.append(rejection)
                continue

            snapshot = self._health_snapshot(registration.provider_id)
            if snapshot.status is ProviderHealthStatus.UNHEALTHY:
                rejected.append(
                    SelectionRejection(
                        provider_id=registration.provider_id,
                        reason_code=EligibilityRejectionCode.UNHEALTHY,
                        details={
                            "health_source": snapshot.source,
                            "health_reason_code": snapshot.reason_code,
                        },
                    )
                )
                continue

            if (
                context.provider_pin is not None
                and registration.provider_id != context.provider_pin.provider_id
            ):
                rejected.append(
                    SelectionRejection(
                        provider_id=registration.provider_id,
                        reason_code=EligibilityRejectionCode.PIN_MISMATCH,
                        details={"pinned_provider_id": context.provider_pin.provider_id},
                    )
                )
                continue

            eligible.append(EligibleProvider(registration=registration, health=snapshot))

        return EligibilityResult(eligible=tuple(eligible), rejected=tuple(rejected))

    def _evaluate_candidate(
        self,
        registration: ProviderRegistration,
        context: SelectionContext,
        constraints: SelectionPolicyConstraints,
    ) -> SelectionRejection | None:
        descriptor = registration.descriptor
        if registration.capability.id != context.capability_id:
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.CAPABILITY_MISMATCH,
                details={
                    "candidate_capability_id": registration.capability.id,
                    "requested_capability_id": context.capability_id,
                },
            )

        if (
            constraints.allowed_provider_ids is not None
            and registration.provider_id not in constraints.allowed_provider_ids
        ):
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.PROVIDER_NOT_ALLOWED,
            )
        if registration.provider_id in constraints.denied_provider_ids:
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.PROVIDER_DENIED,
            )

        if (
            constraints.required_region is not None
            and descriptor.region != constraints.required_region
        ):
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.REGION_MISMATCH,
                details={"required_region": constraints.required_region},
            )
        if (
            constraints.allowed_regions is not None
            and descriptor.region not in constraints.allowed_regions
        ):
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.REGION_MISMATCH,
                details={"allowed_regions": sorted(constraints.allowed_regions)},
            )
        if not constraints.required_provider_tags.issubset(descriptor.tags):
            return SelectionRejection(
                provider_id=registration.provider_id,
                reason_code=EligibilityRejectionCode.REQUIRED_TAG_MISSING,
                details={
                    "required_provider_tags": sorted(constraints.required_provider_tags),
                },
            )

        if descriptor.tenant_visibility:
            if context.tenant_id is None or context.tenant_id not in descriptor.tenant_visibility:
                return SelectionRejection(
                    provider_id=registration.provider_id,
                    reason_code=EligibilityRejectionCode.TENANT_NOT_ALLOWED,
                )
        return None

    def _health_snapshot(self, provider_id: str) -> ProviderHealthSnapshot:
        try:
            snapshot = self._health_source.snapshot(provider_id)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "provider health lookup failed",
                code=ErrorCode.PROVIDER_HEALTH_UNAVAILABLE,
                details={
                    "provider_id": provider_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc

        if not isinstance(snapshot, ProviderHealthSnapshot):
            raise ProviderError(
                "health source must return ProviderHealthSnapshot",
                code=ErrorCode.PROVIDER_HEALTH_UNAVAILABLE,
                details={"provider_id": provider_id},
            )
        if snapshot.provider_id != provider_id:
            raise ProviderError(
                "health snapshot provider_id mismatch",
                code=ErrorCode.PROVIDER_HEALTH_UNAVAILABLE,
                details={
                    "provider_id": provider_id,
                    "snapshot_provider_id": snapshot.provider_id,
                },
            )
        return snapshot

    @staticmethod
    def _parse_constraints(context: SelectionContext) -> SelectionPolicyConstraints:
        try:
            return SelectionPolicyConstraints.model_validate(dict(context.policy_constraints))
        except Exception as exc:
            raise SelectionError(
                "selection policy constraints are invalid",
                code=ErrorCode.SELECTION_INVALID_CONTEXT,
                details={"cause_type": type(exc).__name__},
            ) from exc
