"""Context candidate 规范化、去重、固定排序与 Snapshot 物化。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from uuid import uuid4

from harness_contracts import (
    ContextError,
    ContextItem,
    ContextSnapshot,
    ContextSourceKind,
    ContextTrustTier,
    ErrorCode,
)

from .canonical import canonical_hash, context_item_facts

type IdFactory = Callable[[], str]

_TRUST_ORDER = {
    ContextTrustTier.SYSTEM: 0,
    ContextTrustTier.APPLICATION: 1,
    ContextTrustTier.USER: 2,
    ContextTrustTier.DATA: 3,
}
_SOURCE_ORDER = {source: index for index, source in enumerate(ContextSourceKind)}


class ContextAssembler:
    def __init__(self, *, snapshot_id_factory: IdFactory | None = None) -> None:
        if snapshot_id_factory is not None and not callable(snapshot_id_factory):
            raise TypeError("snapshot_id_factory must be callable")
        self._snapshot_id_factory = snapshot_id_factory or (
            lambda: f"context-snapshot-{uuid4().hex}"
        )

    def normalize(self, candidates: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
        if isinstance(candidates, ContextItem):
            raise TypeError("candidates must be an iterable of ContextItem values")
        unique: dict[str, ContextItem] = {}
        facts_by_id: dict[str, str] = {}
        for candidate in candidates:
            if not isinstance(candidate, ContextItem):
                raise TypeError("candidates must contain ContextItem values")
            facts = canonical_hash(context_item_facts(candidate))
            previous = facts_by_id.get(candidate.item_id)
            if previous is not None and previous != facts:
                raise ContextError(
                    "context item identity maps to conflicting source facts",
                    code=ErrorCode.CONTEXT_INVALID,
                    details={
                        "reason": "conflicting_item_identity",
                        "item_id": candidate.item_id,
                    },
                )
            if previous is None:
                unique[candidate.item_id] = candidate
                facts_by_id[candidate.item_id] = facts

        items = tuple(sorted(unique.values(), key=_item_order))
        if len(items) > 256:
            raise ContextError(
                "context candidate set exceeds snapshot capacity",
                code=ErrorCode.CONTEXT_INVALID,
                details={"reason": "snapshot_capacity_exceeded", "item_count": len(items)},
            )
        return items

    def materialize_snapshot(
        self,
        items: tuple[ContextItem, ...],
        *,
        created_at: datetime,
    ) -> ContextSnapshot:
        normalized = self.normalize(items)
        return ContextSnapshot(
            snapshot_id=self._snapshot_id_factory(),
            items=normalized,
            canonical_hash=canonical_hash([context_item_facts(item) for item in normalized]),
            created_at=created_at,
        )


def _item_order(item: ContextItem) -> tuple[int, int, str]:
    return (
        _TRUST_ORDER[item.trust_tier],
        _SOURCE_ORDER[item.source.source_kind],
        item.item_id,
    )
