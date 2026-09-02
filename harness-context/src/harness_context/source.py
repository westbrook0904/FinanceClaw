"""Foundation F2 的 ContextSource SPI 与基础来源适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from datetime import datetime

from harness_contracts import (
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


class ObservationContextSource(ContextSource):
    """把已完成 Action 的有界 Observation 投影给后续 Explore turn。"""

    @property
    def source_name(self) -> str:
        return "harness-context.observation"

    def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> tuple[ContextItem, ...]:
        if consumer is not ContextConsumer.EXPLORE:
            return ()
        items: list[ContextItem] = []
        for observation in collection.observations:
            content = {
                "observation_id": observation.observation_id,
                "action_id": observation.action_id,
                "result_status": observation.result_status.value,
                "bounded_summary": observation.model_dump(mode="json")["bounded_summary"],
                "evidence_refs": list(observation.evidence_refs),
                "result_hash": observation.result_hash,
            }
            source_version = canonical_hash(content)
            items.append(
                ContextItem(
                    item_id=stable_item_id(
                        source_kind=ContextSourceKind.OBSERVATION.value,
                        source_id=observation.observation_id,
                        source_version=source_version,
                        kind="observation",
                    ),
                    kind="observation",
                    content=content,
                    source=ContextSourceRef(
                        source_kind=ContextSourceKind.OBSERVATION,
                        source_id=observation.observation_id,
                    ),
                    provenance=ContextProvenance(
                        producer=self.source_name,
                        evidence_refs=observation.evidence_refs,
                    ),
                    freshness=ContextFreshness(
                        source_version=source_version,
                        observed_at=observed_at,
                    ),
                    trust_tier=ContextTrustTier.DATA,
                    sensitivity=ContextSensitivity.CONFIDENTIAL,
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
