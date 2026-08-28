"""Trace、Span 与 Event 的观测抽象。

Runtime 只依赖本包的 ``Tracer`` SPI，不直接依赖 OpenTelemetry 或其他厂商 SDK。
"""

from .console import ConsoleTracer
from .models import Span, SpanStatus, SpanType, TraceError, TraceEvent
from .tracer import InMemoryTracer, Tracer, TraceStateError

__all__ = [
    "ConsoleTracer",
    "InMemoryTracer",
    "Span",
    "SpanStatus",
    "SpanType",
    "TraceError",
    "TraceEvent",
    "TraceStateError",
    "Tracer",
]
