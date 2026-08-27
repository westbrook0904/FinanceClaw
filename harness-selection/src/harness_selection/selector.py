"""ProviderSelector SPI 与确定性的 PrioritySelector。"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence

from harness_contracts import (
    ErrorCode,
    ProviderError,
    ProviderHealthStatus,
    SelectionContext,
    SelectionDecision,
)
from harness_registry import ProviderRegistration

from .eligibility import EligibilityPipeline, EligibilityResult, EligibleProvider


_HEALTH_RANK = {
    ProviderHealthStatus.HEALTHY: 0,
    ProviderHealthStatus.UNKNOWN: 1,
    ProviderHealthStatus.DEGRADED: 2,
}


class ProviderSelector(ABC):
    """从已发现候选中返回一次稳定 SelectionDecision。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Selector 的稳定名称。"""

    @abstractmethod
    def select(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
    ) -> SelectionDecision:
        """选择一个 Provider；无安全候选时抛出结构化 ProviderError。"""


class PrioritySelector(ProviderSelector):
    """Health → priority → provider_id 的确定性 Selector。"""

    def __init__(self, eligibility: EligibilityPipeline | None = None) -> None:
        effective_eligibility = eligibility or EligibilityPipeline()
        if not isinstance(effective_eligibility, EligibilityPipeline):
            raise TypeError("eligibility must be EligibilityPipeline")
        self._eligibility = effective_eligibility

    @property
    def name(self) -> str:
        return "priority"

    @property
    def eligibility(self) -> EligibilityPipeline:
        return self._eligibility

    def select(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
    ) -> SelectionDecision:
        if not isinstance(context, SelectionContext):
            raise TypeError("context must be SelectionContext")
        candidate_tuple = tuple(candidates)
        if any(not isinstance(item, ProviderRegistration) for item in candidate_tuple):
            raise TypeError("candidates must contain ProviderRegistration values")

        if context.provider_pin is not None:
            all_provider_ids = {item.provider_id for item in candidate_tuple}
            if context.provider_pin.provider_id not in all_provider_ids:
                raise ProviderError(
                    "pinned provider is not present in candidate set",
                    code=ErrorCode.PROVIDER_PIN_NOT_FOUND,
                    details={
                        "capability_id": context.capability_id,
                        "provider_id": context.provider_pin.provider_id,
                    },
                )

        result = self._eligibility.evaluate(candidate_tuple, context)
        if context.provider_pin is not None and not result.eligible:
            rejection = next(
                (
                    item
                    for item in result.rejected
                    if item.provider_id == context.provider_pin.provider_id
                ),
                None,
            )
            raise ProviderError(
                "pinned provider is not eligible",
                code=ErrorCode.PROVIDER_PIN_NOT_ALLOWED,
                details={
                    "capability_id": context.capability_id,
                    "provider_id": context.provider_pin.provider_id,
                    "reason_code": rejection.reason_code if rejection is not None else None,
                },
            )
        if not result.eligible:
            raise ProviderError(
                "no eligible provider candidate",
                code=ErrorCode.PROVIDER_NO_ELIGIBLE_CANDIDATE,
                details={
                    "capability_id": context.capability_id,
                    "rejected": [
                        {
                            "provider_id": item.provider_id,
                            "reason_code": item.reason_code,
                        }
                        for item in result.rejected
                    ],
                },
            )

        ranked = tuple(sorted(result.eligible, key=_priority_sort_key))
        selected = ranked[0]
        reason_code = _selection_reason(context, ranked)
        return SelectionDecision(
            capability_id=context.capability_id,
            selected_provider_id=selected.provider_id,
            eligible_candidates=tuple(item.provider_id for item in ranked),
            rejected_candidates=result.rejected,
            selector=self.name,
            reason_code=reason_code,
            selection_key=_selection_key(self.name, context, ranked, result),
        )


def _priority_sort_key(item: EligibleProvider) -> tuple[int, int, str]:
    return (
        _HEALTH_RANK[item.health.status],
        -item.registration.descriptor.priority,
        item.provider_id,
    )


def _selection_reason(
    context: SelectionContext,
    ranked: tuple[EligibleProvider, ...],
) -> str:
    if context.provider_pin is not None:
        return "PROVIDER_PINNED"
    if len(ranked) == 1:
        return "ONLY_ELIGIBLE_PROVIDER"
    return "HEALTH_PRIORITY_ORDER"


def _selection_key(
    selector: str,
    context: SelectionContext,
    ranked: tuple[EligibleProvider, ...],
    result: EligibilityResult,
) -> str:
    payload = {
        "selector": selector,
        "request_id": context.request_id,
        "capability_id": context.capability_id,
        "tenant_id": context.tenant_id,
        "provider_pin": (
            context.provider_pin.provider_id if context.provider_pin is not None else None
        ),
        "eligible": [
            {
                "provider_id": item.provider_id,
                "health": item.health.status.value,
                "priority": item.registration.descriptor.priority,
            }
            for item in ranked
        ],
        "rejected": [
            {"provider_id": item.provider_id, "reason_code": item.reason_code}
            for item in result.rejected
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
