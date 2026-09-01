"""把受治理、受裁剪的 MemorySlice 适配为 DATA tier ContextItem。"""

from __future__ import annotations

from collections.abc import Iterable
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
    ErrorCode,
    MemoryAccessError,
    MemoryKind,
    MemoryRecord,
)
from harness_memory import MemoryGateway

from .canonical import canonical_hash, stable_item_id
from .models import ContextCollection
from .source import ContextSource


class MemoryContextSource(ContextSource):
    """只通过 MemoryGateway 读取当前可信 subject 的长期事实。"""

    def __init__(
        self,
        gateway: MemoryGateway,
        *,
        namespaces: Iterable[str],
        kinds: Iterable[MemoryKind] | None = None,
        tags: Iterable[str] = (),
        text: str | None = None,
        limit: int = 20,
        required: bool = False,
    ) -> None:
        if not isinstance(gateway, MemoryGateway):
            raise TypeError("gateway must be MemoryGateway")
        if not isinstance(required, bool):
            raise TypeError("required must be bool")
        self._gateway = gateway
        self._namespaces = _validated_values("namespaces", namespaces, required=True)
        if not self._namespaces.issubset(gateway.allowed_namespaces):
            raise ValueError("memory context namespaces must be allowed by the gateway")
        self._kinds = None if kinds is None else frozenset(kinds)
        if self._kinds is not None and (
            not self._kinds or any(not isinstance(kind, MemoryKind) for kind in self._kinds)
        ):
            raise TypeError("kinds must contain MemoryKind values")
        self._tags = _validated_values("tags", tags, required=False)
        if text is not None and not isinstance(text, str):
            raise TypeError("text must be a string or None")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer")
        self._text = text
        self._limit = limit
        self._required = required

    @property
    def source_name(self) -> str:
        return "harness-context.memory"

    @property
    def gateway(self) -> MemoryGateway:
        return self._gateway

    async def collect(
        self,
        collection: ContextCollection,
        consumer: ContextConsumer,
        *,
        observed_at: datetime,
    ) -> tuple[ContextItem, ...]:
        del consumer
        try:
            query = self._gateway.create_query(
                collection.invocation,
                namespaces=self._namespaces,
                kinds=self._kinds,
                tags=self._tags,
                text=self._text,
                limit=self._limit,
            )
            memory_slice = await self._gateway.search(collection.invocation, query)
        except MemoryAccessError as exc:
            if not self._required and exc.code == ErrorCode.MEMORY_TRUSTED_SCOPE_REQUIRED.value:
                return ()
            raise
        return tuple(
            _record_to_context_item(record, observed_at=observed_at)
            for record in memory_slice.records
        )


def _record_to_context_item(
    record: MemoryRecord,
    *,
    observed_at: datetime,
) -> ContextItem:
    record_facts = record.model_dump(mode="json")
    record_facts["tags"] = sorted(record.tags)
    source_version = canonical_hash(record_facts)
    content = {
        "kind": record.kind.value,
        "namespace": record.namespace,
        "source_fact_hash": record.provenance.source_fact_hash,
        "tags": sorted(record.tags),
        "value": record.content,
    }
    return ContextItem(
        item_id=stable_item_id(
            source_kind=ContextSourceKind.MEMORY.value,
            source_id=record.memory_id,
            source_version=source_version,
            kind=f"memory:{record.kind.value}",
        ),
        kind=f"memory:{record.kind.value}",
        content=content,
        source=ContextSourceRef(
            source_kind=ContextSourceKind.MEMORY,
            source_id=record.memory_id,
        ),
        provenance=ContextProvenance(
            producer=record.provenance.producer,
            evidence_refs=record.provenance.evidence_refs,
        ),
        freshness=ContextFreshness(
            source_version=source_version,
            observed_at=observed_at,
        ),
        trust_tier=ContextTrustTier.DATA,
        sensitivity=ContextSensitivity(record.sensitivity.value),
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


def _validated_values(
    field_name: str,
    values: Iterable[str],
    *,
    required: bool,
) -> frozenset[str]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be an iterable of strings")
    result = frozenset(values)
    if required and not result:
        raise ValueError(f"{field_name} must not be empty")
    if any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in result
    ):
        raise TypeError(f"{field_name} must contain non-empty trimmed strings")
    return result
