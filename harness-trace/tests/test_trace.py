"""harness-trace 的阶段一行为测试。"""

from __future__ import annotations

import io
import json
import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from harness_contracts import PolicyError, TraceContext
from harness_trace import (
    ConsoleTracer,
    InMemoryTracer,
    Span,
    SpanStatus,
    SpanType,
    TraceStateError,
)


class SequenceClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(milliseconds=1)
        return value


class IdSequence:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


def make_tracer() -> InMemoryTracer:
    return InMemoryTracer(
        clock=SequenceClock(),
        trace_id_factory=IdSequence("trace"),
        span_id_factory=IdSequence("span"),
    )


class SpanModelTests(unittest.TestCase):
    def test_span_is_frozen(self) -> None:
        span = make_tracer().start_span("request", SpanType.REQUEST)
        with self.assertRaises(ValidationError):
            span.name = "changed"  # type: ignore[misc]

    def test_span_rejects_naive_timestamp(self) -> None:
        with self.assertRaises(ValidationError):
            Span(
                trace_id="trace",
                span_id="span",
                type=SpanType.REQUEST,
                name="request",
                start_time=datetime.now(),
            )


class InMemoryTracerTests(unittest.TestCase):
    def test_root_and_child_share_trace_and_form_parent_chain(self) -> None:
        tracer = make_tracer()
        request = tracer.start_span("request", SpanType.REQUEST)
        runtime = tracer.start_span("runtime", SpanType.RUNTIME, parent=request)
        policy = tracer.start_span("policy.pre_execute", SpanType.POLICY, parent=runtime)

        self.assertEqual(request.trace_id, runtime.trace_id)
        self.assertEqual(runtime.parent_span_id, request.span_id)
        self.assertEqual(policy.parent_span_id, runtime.span_id)

    def test_documented_phase_one_hierarchy_can_be_represented(self) -> None:
        tracer = make_tracer()
        request = tracer.start_span("request", SpanType.REQUEST)
        runtime = tracer.start_span("runtime", SpanType.RUNTIME, parent=request)
        capability = tracer.start_span("capability", SpanType.CAPABILITY, parent=runtime)
        agent = tracer.start_span("agent.finance-query", SpanType.AGENT, parent=capability)
        tool = tracer.start_span("tool.metric-query", SpanType.TOOL, parent=agent)

        self.assertEqual(tool.trace_id, request.trace_id)
        self.assertEqual(tool.parent_span_id, agent.span_id)
        self.assertEqual(
            [span.type for span in tracer.spans(trace_id=request.trace_id)],
            [
                SpanType.REQUEST,
                SpanType.RUNTIME,
                SpanType.CAPABILITY,
                SpanType.AGENT,
                SpanType.TOOL,
            ],
        )

    def test_external_trace_context_continues_existing_trace(self) -> None:
        tracer = make_tracer()
        parent = TraceContext(trace_id="incoming-trace", span_id="remote-parent")

        span = tracer.start_span("request", SpanType.REQUEST, parent=parent)

        self.assertEqual(span.trace_id, "incoming-trace")
        self.assertEqual(span.parent_span_id, "remote-parent")

    def test_add_event_and_filter_snapshots(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("runtime", SpanType.RUNTIME)
        event = tracer.add_event(span, "registry.resolve", attributes={"hit": True})

        self.assertEqual(event.trace_id, span.trace_id)
        self.assertEqual(tracer.events(span_id=span.span_id), (event,))
        self.assertEqual(tracer.spans(trace_id=span.trace_id), (span,))

    def test_end_span_merges_attributes(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("runtime", SpanType.RUNTIME, attributes={"request_id": "r1"})

        finished = tracer.end_span(span, attributes={"provider": "echo"})

        self.assertEqual(finished.status, SpanStatus.OK)
        self.assertIsNotNone(finished.end_time)
        self.assertEqual(
            finished.model_dump(mode="json")["attributes"],
            {"request_id": "r1", "provider": "echo"},
        )
        self.assertEqual(tracer.get_span(span.span_id), finished)

    def test_error_end_normalizes_harness_error(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("policy", SpanType.POLICY)

        finished = tracer.end_span(
            span,
            status=SpanStatus.ERROR,
            error=PolicyError("denied"),
        )

        self.assertEqual(finished.error.type, "PolicyError")
        self.assertEqual(finished.error.code, "HARNESS.POLICY.DENIED")

    def test_error_status_requires_error(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("runtime", SpanType.RUNTIME)
        with self.assertRaises(ValueError):
            tracer.end_span(span, status=SpanStatus.ERROR)

    def test_success_status_rejects_error(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("runtime", SpanType.RUNTIME)
        with self.assertRaises(ValueError):
            tracer.end_span(span, error=RuntimeError("boom"))

    def test_finished_span_rejects_more_events_or_second_end(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("runtime", SpanType.RUNTIME)
        tracer.end_span(span)

        with self.assertRaises(TraceStateError):
            tracer.add_event(span, "late")
        with self.assertRaises(TraceStateError):
            tracer.end_span(span)

    def test_finished_parent_rejects_new_child(self) -> None:
        tracer = make_tracer()
        parent = tracer.start_span("runtime", SpanType.RUNTIME)
        tracer.end_span(parent)

        with self.assertRaises(TraceStateError):
            tracer.start_span("tool", SpanType.TOOL, parent=parent)

    def test_trace_context_uses_current_span(self) -> None:
        tracer = make_tracer()
        span = tracer.start_span("agent", SpanType.AGENT)

        context = tracer.trace_context(span)

        self.assertEqual(context.trace_id, span.trace_id)
        self.assertEqual(context.span_id, span.span_id)


class ConsoleTracerTests(unittest.TestCase):
    def test_console_tracer_emits_json_lines_for_lifecycle(self) -> None:
        stream = io.StringIO()
        tracer = ConsoleTracer(
            stream=stream,
            clock=SequenceClock(),
            trace_id_factory=IdSequence("trace"),
            span_id_factory=IdSequence("span"),
        )

        span = tracer.start_span("request", SpanType.REQUEST)
        tracer.add_event(span, "accepted")
        tracer.end_span(span)

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(
            [record["event"] for record in records],
            ["span.start", "span.event", "span.end"],
        )
        self.assertEqual(records[0]["span"]["span_id"], span.span_id)
        self.assertEqual(records[1]["trace_event"]["name"], "accepted")
        self.assertEqual(records[2]["span"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
