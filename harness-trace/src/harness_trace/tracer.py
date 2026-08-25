"""Tracer SPI 与阶段一内存实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from harness_contracts import HarnessError, JsonValue, TraceContext

from .models import Span, SpanStatus, SpanType, TraceError, TraceEvent

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]
type SpanRef = Span | str
type TraceParent = Span | TraceContext | None


class TraceStateError(RuntimeError):
    """Tracer 生命周期被非法使用时抛出的本地异常。"""


class Tracer(ABC):
    """Runtime 依赖的最小 Trace SPI。"""

    @abstractmethod
    def start_span(
        self,
        name: str,
        span_type: SpanType,
        *,
        parent: TraceParent = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> Span:
        """创建并返回一个 RUNNING Span。"""

    @abstractmethod
    def add_event(
        self,
        span: SpanRef,
        name: str,
        *,
        attributes: dict[str, JsonValue] | None = None,
    ) -> TraceEvent:
        """向仍在运行的 Span 添加结构化事件。"""

    @abstractmethod
    def end_span(
        self,
        span: SpanRef,
        *,
        status: SpanStatus = SpanStatus.OK,
        error: BaseException | TraceError | str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> Span:
        """结束 Span，并返回最终不可变快照。"""


class InMemoryTracer(Tracer):
    """线程安全的阶段一 Tracer，保存 Span 与 Event 快照供 Runtime/测试查询。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        trace_id_factory: IdFactory | None = None,
        span_id_factory: IdFactory | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._trace_id_factory = trace_id_factory or (lambda: uuid4().hex)
        self._span_id_factory = span_id_factory or (lambda: uuid4().hex[:16])
        self._spans: dict[str, Span] = {}
        self._events: list[TraceEvent] = []
        self._lock = RLock()

    def start_span(
        self,
        name: str,
        span_type: SpanType,
        *,
        parent: TraceParent = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> Span:
        with self._lock:
            trace_id, parent_span_id = self._resolve_parent(parent)
            span = Span(
                trace_id=trace_id or self._new_unique_trace_id(),
                span_id=self._new_unique_span_id(),
                parent_span_id=parent_span_id,
                type=span_type,
                name=name,
                start_time=self._now(),
                attributes=attributes or {},
            )
            self._spans[span.span_id] = span
        self._on_span_started(span)
        return span

    def add_event(
        self,
        span: SpanRef,
        name: str,
        *,
        attributes: dict[str, JsonValue] | None = None,
    ) -> TraceEvent:
        with self._lock:
            current = self._require_running(span)
            event = TraceEvent(
                trace_id=current.trace_id,
                span_id=current.span_id,
                name=name,
                timestamp=self._now(),
                attributes=attributes or {},
            )
            self._events.append(event)
        self._on_event_added(event)
        return event

    def end_span(
        self,
        span: SpanRef,
        *,
        status: SpanStatus = SpanStatus.OK,
        error: BaseException | TraceError | str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> Span:
        if status is SpanStatus.RUNNING:
            raise ValueError("end_span status cannot be running")
        if status is SpanStatus.ERROR and error is None:
            raise ValueError("error status requires error")
        if status is not SpanStatus.ERROR and error is not None:
            raise ValueError("error may only be supplied with error status")

        with self._lock:
            current = self._require_running(span)
            merged_attributes = current.model_dump(mode="json")["attributes"]
            if attributes:
                merged_attributes.update(attributes)

            finished = current.model_copy(
                update={
                    "end_time": self._now(),
                    "attributes": merged_attributes,
                    "status": status,
                    "error": _normalize_error(error),
                }
            )
            finished = Span.model_validate(finished.model_dump(mode="python"))
            self._spans[finished.span_id] = finished
        self._on_span_ended(finished)
        return finished

    def get_span(self, span_id: str) -> Span | None:
        """按 span_id 返回当前快照，不存在时返回 ``None``。"""

        with self._lock:
            return self._spans.get(span_id)

    def spans(self, *, trace_id: str | None = None) -> tuple[Span, ...]:
        """按开始时间返回 Span 快照；可限定单个 trace。"""

        with self._lock:
            spans = tuple(self._spans.values())
        if trace_id is not None:
            spans = tuple(span for span in spans if span.trace_id == trace_id)
        return tuple(sorted(spans, key=lambda item: (item.start_time, item.span_id)))

    def events(
        self,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> tuple[TraceEvent, ...]:
        """返回事件快照，可按 trace_id / span_id 过滤。"""

        with self._lock:
            events = tuple(self._events)
        if trace_id is not None:
            events = tuple(event for event in events if event.trace_id == trace_id)
        if span_id is not None:
            events = tuple(event for event in events if event.span_id == span_id)
        return events

    def trace_context(self, span: SpanRef) -> TraceContext:
        """把本地 Span 转成可跨模块传播的 ``TraceContext``。"""

        current = self._require_span(span)
        return TraceContext(
            trace_id=current.trace_id,
            span_id=current.span_id,
            parent_span_id=current.parent_span_id,
        )

    def _resolve_parent(self, parent: TraceParent) -> tuple[str | None, str | None]:
        if parent is None:
            return None, None
        if isinstance(parent, Span):
            current = self._require_span(parent)
            if current.status is not SpanStatus.RUNNING:
                raise TraceStateError("cannot start child span from a finished parent")
            return current.trace_id, current.span_id
        if isinstance(parent, TraceContext):
            return parent.trace_id, parent.span_id
        raise TypeError("parent must be Span, TraceContext, or None")

    def _require_span(self, span: SpanRef) -> Span:
        span_id = span.span_id if isinstance(span, Span) else span
        with self._lock:
            current = self._spans.get(span_id)
        if current is None:
            raise TraceStateError(f"span not found: {span_id}")
        return current

    def _require_running(self, span: SpanRef) -> Span:
        current = self._require_span(span)
        if current.status is not SpanStatus.RUNNING:
            raise TraceStateError(f"span already finished: {current.span_id}")
        return current

    def _new_unique_trace_id(self) -> str:
        for _ in range(100):
            value = self._trace_id_factory().strip()
            if not value:
                raise ValueError("trace_id_factory must return a non-empty id")
            with self._lock:
                if all(span.trace_id != value for span in self._spans.values()):
                    return value
        raise TraceStateError("trace_id_factory produced too many duplicate ids")

    def _new_unique_span_id(self) -> str:
        for _ in range(100):
            value = self._span_id_factory().strip()
            if not value:
                raise ValueError("span_id_factory must return a non-empty id")
            with self._lock:
                if value not in self._spans:
                    return value
        raise TraceStateError("span_id_factory produced too many duplicate ids")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trace clock must return timezone-aware datetime")
        return value

    def _on_span_started(self, span: Span) -> None:
        """实现类可覆写的观察钩子。"""

    def _on_event_added(self, event: TraceEvent) -> None:
        """实现类可覆写的观察钩子。"""

    def _on_span_ended(self, span: Span) -> None:
        """实现类可覆写的观察钩子。"""


def _normalize_error(error: BaseException | TraceError | str | None) -> TraceError | None:
    if error is None:
        return None
    if isinstance(error, TraceError):
        return error
    if isinstance(error, HarnessError):
        return TraceError(
            type=type(error).__name__,
            message=error.message,
            code=error.code,
        )
    if isinstance(error, BaseException):
        return TraceError(type=type(error).__name__, message=str(error) or type(error).__name__)
    return TraceError(type="Error", message=error)
