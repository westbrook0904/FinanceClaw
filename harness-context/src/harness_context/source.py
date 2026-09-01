"""Foundation F2 的 ContextSource SPI 与基础来源适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from datetime import datetime

from harness_contracts import (
    CapabilityDescriptor,
    ContextConsumer,
    ContextFreshness,
    ContextItem,
    ContextProvenance,
    ContextSensitivity,
    ContextSourceKind,
    ContextSourceRef,
    ContextTrustTier,
    ContractModel,
)
from harness_contracts.base import NonEmptyString
from pydantic import Field

from .canonical import canonical_hash, stable_item_id
from .models import ContextCollection

type ContextSourceResult = tuple[ContextItem, ...] | Awaitable[tuple[ContextItem, ...]]


class ContextSource(ABC):
    """收集瞬时 candidate item；实现不得写 StateStore 或 Trace。"""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """返回用于 provenance 的稳定来源名称。"""

    @abstractmethod
    def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> ContextSourceResult:
        """返回带稳定 item identity 的候选项。"""


class RequestContextSource(ContextSource):
    @property
    def source_name(self) -> str:
        return "harness-context.request"

    def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> tuple[ContextItem, ...]:
        del consumer
        content = collection.request_projection
        source_id = collection.invocation.request.request_id
        source_version = canonical_hash(content)
        return (
            ContextItem(
                item_id=stable_item_id(
                    source_kind=ContextSourceKind.REQUEST.value,
                    source_id=source_id,
                    source_version=source_version,
                    kind="request",
                ),
                kind="request",
                content=content,
                source=ContextSourceRef(
                    source_kind=ContextSourceKind.REQUEST,
                    source_id=source_id,
                ),
                provenance=ContextProvenance(producer=self.source_name),
                freshness=ContextFreshness(
                    source_version=source_version,
                    observed_at=observed_at,
                ),
                trust_tier=ContextTrustTier.USER,
                sensitivity=ContextSensitivity.CONFIDENTIAL,
                created_at=observed_at,
            ),
        )


class CapabilityCatalogContextSource(ContextSource):
    @property
    def source_name(self) -> str:
        return "harness-context.capability-catalog"

    def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> tuple[ContextItem, ...]:
        items: list[ContextItem] = []
        for descriptor in collection.capability_catalog:
            content = _project_descriptor(descriptor, consumer)
            source_version = canonical_hash(content)
            items.append(
                ContextItem(
                    item_id=stable_item_id(
                        source_kind=ContextSourceKind.CAPABILITY_CATALOG.value,
                        source_id=descriptor.id,
                        source_version=source_version,
                        kind="capability",
                    ),
                    kind="capability",
                    content=content,
                    source=ContextSourceRef(
                        source_kind=ContextSourceKind.CAPABILITY_CATALOG,
                        source_id=descriptor.id,
                    ),
                    provenance=ContextProvenance(producer=self.source_name),
                    freshness=ContextFreshness(
                        source_version=source_version,
                        observed_at=observed_at,
                    ),
                    trust_tier=ContextTrustTier.APPLICATION,
                    sensitivity=ContextSensitivity.INTERNAL,
                    created_at=observed_at,
                )
            )
        return tuple(items)


class StaticContextEntry(ContractModel):
    source_id: NonEmptyString
    content: NonEmptyString
    consumers: frozenset[ContextConsumer] = Field(
        default_factory=lambda: frozenset(ContextConsumer)
    )
    sensitivity: ContextSensitivity = ContextSensitivity.INTERNAL


class StaticContextSource(ContextSource):
    def __init__(self, entries: tuple[StaticContextEntry, ...]) -> None:
        if any(not isinstance(entry, StaticContextEntry) for entry in entries):
            raise TypeError("entries must contain StaticContextEntry values")
        source_ids = [entry.source_id for entry in entries]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("static context source IDs must be unique")
        self._entries = entries

    @property
    def source_name(self) -> str:
        return "harness-context.static"

    def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> tuple[ContextItem, ...]:
        del collection
        items: list[ContextItem] = []
        for entry in self._entries:
            if consumer not in entry.consumers:
                continue
            source_version = canonical_hash(entry.content)
            items.append(
                ContextItem(
                    item_id=stable_item_id(
                        source_kind=ContextSourceKind.SYSTEM_INSTRUCTION.value,
                        source_id=entry.source_id,
                        source_version=source_version,
                        kind="system_instruction",
                    ),
                    kind="system_instruction",
                    content=entry.content,
                    source=ContextSourceRef(
                        source_kind=ContextSourceKind.SYSTEM_INSTRUCTION,
                        source_id=entry.source_id,
                    ),
                    provenance=ContextProvenance(producer=self.source_name),
                    freshness=ContextFreshness(
                        source_version=source_version,
                        observed_at=observed_at,
                    ),
                    trust_tier=ContextTrustTier.SYSTEM,
                    sensitivity=entry.sensitivity,
                    created_at=observed_at,
                )
            )
        return tuple(items)


def _project_descriptor(
    descriptor: CapabilityDescriptor,
    consumer: ContextConsumer,
) -> dict[str, object]:
    if not isinstance(descriptor, CapabilityDescriptor):
        raise TypeError("capability catalog must contain CapabilityDescriptor values")
    payload = descriptor.model_dump(mode="json")
    projected: dict[str, object] = {
        "id": payload["id"],
        "name": payload["name"],
        "type": payload["type"],
        "version": payload["version"],
        "tags": sorted(payload["tags"]),
    }
    if consumer in {ContextConsumer.PLAN, ContextConsumer.EXPLORE}:
        execution_profile = dict(payload["execution_profile"])
        if consumer is ContextConsumer.PLAN:
            # PLAN supports both sync and async nodes; Explore completion eligibility is a
            # Harness-owned scope guard and must not perturb the established PLAN projection.
            execution_profile.pop("completion_mode", None)
        projected.update(
            {
                "input_schema": payload["input_schema"],
                "output_schema": payload["output_schema"],
                "execution_profile": execution_profile,
            }
        )
    return projected
