"""结构化 JSON Lines ConsoleTracer。"""

from __future__ import annotations

import json
import sys
from threading import RLock
from typing import TextIO

from .models import Span, TraceEvent
from .tracer import InMemoryTracer


class ConsoleTracer(InMemoryTracer):
    """把 Trace 生命周期输出为 JSON Lines，同时保留内存快照。"""

    def __init__(self, *, stream: TextIO | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._stream = stream or sys.stdout
        self._output_lock = RLock()

    def _on_span_started(self, span: Span) -> None:
        self._write("span.start", "span", span.model_dump(mode="json"))

    def _on_event_added(self, event: TraceEvent) -> None:
        self._write("span.event", "trace_event", event.model_dump(mode="json"))

    def _on_span_ended(self, span: Span) -> None:
        self._write("span.end", "span", span.model_dump(mode="json"))

    def _write(self, event: str, payload_name: str, payload: dict) -> None:
        record = {"event": event, payload_name: payload}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._output_lock:
            self._stream.write(line)
            self._stream.flush()
