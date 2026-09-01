"""面向 route/plan/explore 的确定性裁剪与 Projection Hash。"""

from __future__ import annotations

from harness_contracts import (
    ContextConsumer,
    ContextItem,
    ContextOmission,
    ContextOmissionReason,
    ContextProjection,
    ContextProjectionLimits,
    ContextSnapshot,
    ContextSourceKind,
)

from .canonical import canonical_hash, context_item_char_count, context_item_facts

_CONSUMER_SOURCES = {
    ContextConsumer.ROUTE: frozenset(
        {
            ContextSourceKind.SYSTEM_INSTRUCTION,
            ContextSourceKind.REQUEST,
            ContextSourceKind.SESSION,
            ContextSourceKind.MEMORY,
            ContextSourceKind.CAPABILITY_CATALOG,
        }
    ),
    ContextConsumer.PLAN: frozenset(
        {
            ContextSourceKind.SYSTEM_INSTRUCTION,
            ContextSourceKind.REQUEST,
            ContextSourceKind.SESSION,
            ContextSourceKind.MEMORY,
            ContextSourceKind.CAPABILITY_CATALOG,
        }
    ),
    ContextConsumer.EXPLORE: frozenset(ContextSourceKind),
}


class ContextProjector:
    def project(
        self,
        snapshot: ContextSnapshot,
        consumer: ContextConsumer,
        limits: ContextProjectionLimits,
    ) -> ContextProjection:
        if not isinstance(snapshot, ContextSnapshot):
            raise TypeError("snapshot must be ContextSnapshot")
        if not isinstance(consumer, ContextConsumer):
            raise TypeError("consumer must be ContextConsumer")
        if not isinstance(limits, ContextProjectionLimits):
            raise TypeError("limits must be ContextProjectionLimits")

        included: list[ContextItem] = []
        omitted: list[ContextOmission] = []
        total_chars = 0
        observation_count = 0
        memory_count = 0
        for item in snapshot.items:
            reason: ContextOmissionReason | None = None
            if item.source.source_kind not in _CONSUMER_SOURCES[consumer]:
                reason = ContextOmissionReason.CONSUMER_FILTER
            else:
                item_chars = context_item_char_count(item)
                if item_chars > limits.max_chars_per_item:
                    reason = ContextOmissionReason.ITEM_TOO_LARGE
                elif (
                    item.source.source_kind is ContextSourceKind.OBSERVATION
                    and observation_count >= limits.max_observations
                ):
                    reason = ContextOmissionReason.MAX_OBSERVATIONS
                elif (
                    item.source.source_kind is ContextSourceKind.MEMORY
                    and memory_count >= limits.max_memory_records
                ):
                    reason = ContextOmissionReason.MAX_MEMORY_RECORDS
                elif len(included) >= limits.max_items:
                    reason = ContextOmissionReason.MAX_ITEMS
                elif total_chars + item_chars > limits.max_chars:
                    reason = ContextOmissionReason.MAX_CHARS

            if reason is not None:
                omitted.append(ContextOmission(item_id=item.item_id, reason=reason))
                continue

            included.append(item)
            total_chars += context_item_char_count(item)
            if item.source.source_kind is ContextSourceKind.OBSERVATION:
                observation_count += 1
            if item.source.source_kind is ContextSourceKind.MEMORY:
                memory_count += 1

        projection_hash = canonical_hash(
            {
                "consumer": consumer.value,
                "items": [context_item_facts(item) for item in included],
                "omitted": [item.model_dump(mode="json") for item in omitted],
            }
        )
        return ContextProjection(
            consumer=consumer,
            snapshot_id=snapshot.snapshot_id,
            items=tuple(included),
            omitted=tuple(omitted),
            projection_hash=projection_hash,
        )
