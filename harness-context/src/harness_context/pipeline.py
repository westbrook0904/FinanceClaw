"""ContextSource → Assembler → Policy → Snapshot → Projector 组合入口。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from harness_contracts import (
    ContextConsumer,
    ContextItem,
    ContextProjectionLimits,
    ContextUseRecord,
    InvocationContext,
    JsonValue,
    MemoryAccessError,
    Observation,
)

from .assembler import ContextAssembler
from .models import ContextBundle, ContextCollection
from .policy import ContextPolicy
from .projector import ContextProjector
from .source import ContextSource, RequestContextSource

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]


class ContextPipeline:
    def __init__(
        self,
        policy: ContextPolicy,
        *,
        sources: Iterable[ContextSource] | None = None,
        assembler: ContextAssembler | None = None,
        projector: ContextProjector | None = None,
        limits: Mapping[ContextConsumer, ContextProjectionLimits] | None = None,
        clock: Clock | None = None,
        use_id_factory: IdFactory | None = None,
    ) -> None:
        if not isinstance(policy, ContextPolicy):
            raise TypeError("policy must be ContextPolicy")
        effective_sources = tuple(sources) if sources is not None else (RequestContextSource(),)
        if any(not isinstance(source, ContextSource) for source in effective_sources):
            raise TypeError("sources must contain ContextSource values")
        if assembler is not None and not isinstance(assembler, ContextAssembler):
            raise TypeError("assembler must be ContextAssembler")
        if projector is not None and not isinstance(projector, ContextProjector):
            raise TypeError("projector must be ContextProjector")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if use_id_factory is not None and not callable(use_id_factory):
            raise TypeError("use_id_factory must be callable")

        configured_limits = dict(limits or {})
        if any(not isinstance(key, ContextConsumer) for key in configured_limits):
            raise TypeError("context limit keys must be ContextConsumer values")
        if any(
            not isinstance(value, ContextProjectionLimits) for value in configured_limits.values()
        ):
            raise TypeError("context limits must be ContextProjectionLimits values")

        self._policy = policy
        self._sources = effective_sources
        self._assembler = assembler or ContextAssembler()
        self._projector = projector or ContextProjector()
        self._limits = {
            consumer: configured_limits.get(consumer, ContextProjectionLimits())
            for consumer in ContextConsumer
        }
        self._clock = clock or (lambda: datetime.now(UTC))
        self._use_id_factory = use_id_factory or (lambda: f"context-use-{uuid4().hex}")

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    @property
    def sources(self) -> tuple[ContextSource, ...]:
        return self._sources

    @property
    def assembler(self) -> ContextAssembler:
        return self._assembler

    @property
    def projector(self) -> ContextProjector:
        return self._projector

    async def build(
        self,
        invocation: InvocationContext,
        consumer: ContextConsumer,
        *,
        request_projection: Mapping[str, JsonValue],
        observations: tuple[Observation, ...] = (),
        suppress_memory_errors: bool = False,
    ) -> ContextBundle:
        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        if not isinstance(consumer, ContextConsumer):
            raise TypeError("consumer must be ContextConsumer")
        now = self._clock()
        if not isinstance(suppress_memory_errors, bool):
            raise TypeError("suppress_memory_errors must be bool")
        collection = ContextCollection(
            invocation=invocation,
            request_projection=dict(request_projection),
            observations=observations,
        )
        candidates = []
        source_issues = []
        for source in self._sources:
            try:
                collected = source.collect(collection, consumer, observed_at=now)
                if inspect.isawaitable(collected):
                    collected = await collected
            except MemoryAccessError as exc:
                if not suppress_memory_errors:
                    raise
                source_issues.append(exc.to_detail())
                continue
            if not isinstance(collected, tuple) or any(
                not isinstance(item, ContextItem) for item in collected
            ):
                raise TypeError("ContextSource.collect must return a tuple of ContextItem")
            candidates.extend(collected)

        bundle = self.materialize(
            invocation,
            consumer,
            candidates,
            assembled_at=now,
        )
        if not source_issues:
            return bundle
        return ContextBundle(
            snapshot=bundle.snapshot,
            projection=bundle.projection,
            use_record=bundle.use_record,
            issues=tuple(source_issues),
        )

    def materialize(
        self,
        invocation: InvocationContext,
        consumer: ContextConsumer,
        candidates: Iterable[ContextItem],
        *,
        assembled_at: datetime | None = None,
    ) -> ContextBundle:
        """从已收集的瞬时 candidates 完成 Policy、Snapshot 与 Projection。"""

        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        if not isinstance(consumer, ContextConsumer):
            raise TypeError("consumer must be ContextConsumer")
        now = assembled_at or self._clock()
        normalized = self._assembler.normalize(candidates)
        allowed = self._policy.filter(
            normalized,
            invocation,
            consumer,
            evaluated_at=now,
        )
        snapshot = self._assembler.materialize_snapshot(allowed, created_at=now)
        projection = self._projector.project(snapshot, consumer, self._limits[consumer])
        use_record = ContextUseRecord(
            use_id=self._use_id_factory(),
            consumer=consumer,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.canonical_hash,
            projection_hash=projection.projection_hash,
            included_item_ids=tuple(item.item_id for item in projection.items),
            omitted=projection.omitted,
            assembled_at=now,
        )
        return ContextBundle(
            snapshot=snapshot,
            projection=projection,
            use_record=use_record,
        )
