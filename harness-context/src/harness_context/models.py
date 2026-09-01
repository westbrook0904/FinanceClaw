"""Context Pipeline 的进程内输入与组合结果。"""

from __future__ import annotations

from dataclasses import dataclass

from harness_contracts import (
    CapabilityDescriptor,
    ContextProjection,
    ContextSnapshot,
    ContextUseRecord,
    ContractModel,
    InvocationContext,
)
from harness_contracts.base import FrozenJsonMapping


class ContextCollection(ContractModel):
    invocation: InvocationContext
    request_projection: FrozenJsonMapping
    capability_catalog: tuple[CapabilityDescriptor, ...]


@dataclass(frozen=True, slots=True)
class ContextBundle:
    snapshot: ContextSnapshot
    projection: ContextProjection
    use_record: ContextUseRecord

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ContextSnapshot):
            raise TypeError("snapshot must be ContextSnapshot")
        if not isinstance(self.projection, ContextProjection):
            raise TypeError("projection must be ContextProjection")
        if not isinstance(self.use_record, ContextUseRecord):
            raise TypeError("use_record must be ContextUseRecord")
        if self.projection.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("projection snapshot_id must match snapshot")
        if self.use_record.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("context use snapshot_id must match snapshot")
        if self.use_record.snapshot_hash != self.snapshot.canonical_hash:
            raise ValueError("context use snapshot_hash must match snapshot")
        if self.use_record.consumer is not self.projection.consumer:
            raise ValueError("context use consumer must match projection")
        if self.use_record.projection_hash != self.projection.projection_hash:
            raise ValueError("context use projection_hash must match projection")
        if self.use_record.included_item_ids != tuple(
            item.item_id for item in self.projection.items
        ):
            raise ValueError("context use included items must match projection")
        if self.use_record.omitted != self.projection.omitted:
            raise ValueError("context use omissions must match projection")
        classified_ids = {
            *(item.item_id for item in self.projection.items),
            *(item.item_id for item in self.projection.omitted),
        }
        if classified_ids != {item.item_id for item in self.snapshot.items}:
            raise ValueError("projection must classify every snapshot item")
