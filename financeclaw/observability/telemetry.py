"""OpenTelemetry setup and low-cardinality HTTP/database instrumentation."""

import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import Engine, event

LOGGER = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(slots=True)
class TelemetryRuntime:
    """Own the SDK provider so lifespan shutdown flushes queued spans."""

    trace_provider: TracerProvider | None
    meter_provider: MeterProvider | None

    def shutdown(self) -> None:
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.trace_provider is not None:
            self.trace_provider.shutdown()


def configure_telemetry(
    *,
    service_name: str,
    environment: str,
    endpoint: str | None,
    metrics_endpoint: str | None,
    sample_rate: float,
) -> TelemetryRuntime:
    """Install an OTLP trace provider when an endpoint is configured."""

    if endpoint is None:
        return TelemetryRuntime(trace_provider=None, meter_provider=None)
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment.name": environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    meter_provider = None
    if metrics_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=metrics_endpoint))
        meter_provider = MeterProvider(resource=resource, metric_readers=(reader,))
        metrics.set_meter_provider(meter_provider)
    return TelemetryRuntime(trace_provider=provider, meter_provider=meter_provider)


class _RequestObservabilityMiddleware:
    def __init__(self, app: Any, *, p95_target_ms: int) -> None:
        self.app = app
        self._tracer = trace.get_tracer("financeclaw.api")
        meter = metrics.get_meter("financeclaw.api")
        self._first_byte = meter.create_histogram(
            "financeclaw.http.server.first_byte",
            unit="ms",
            description="BFF time to first response byte",
        )
        self._completion = meter.create_histogram(
            "financeclaw.http.server.duration",
            unit="ms",
            description="BFF request completion latency",
        )
        self._p95_target_ms = p95_target_ms

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        supplied_request_id = headers.get("x-request-id", "")
        request_id = (
            supplied_request_id if _REQUEST_ID.fullmatch(supplied_request_id) else uuid4().hex
        )
        started = perf_counter()
        status_code = 500
        first_byte_recorded = False
        completion_recorded = False
        method = scope.get("method", "UNKNOWN")
        span_name = f"{method} request"
        with self._tracer.start_as_current_span(span_name, kind=trace.SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("financeclaw.request_id", request_id)

            def route_template() -> str:
                route = scope.get("route")
                return getattr(route, "path", "unmatched")

            def record_completion() -> None:
                nonlocal completion_recorded
                if completion_recorded:
                    return
                completion_recorded = True
                duration_ms = (perf_counter() - started) * 1_000
                route = route_template()
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                attributes = {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_code": status_code,
                }
                self._completion.record(duration_ms, attributes)
                LOGGER.info(
                    "http.request.completed",
                    extra={
                        "request_id": request_id,
                        "method": method,
                        "route": route,
                        "status_code": status_code,
                        "duration_ms": round(duration_ms, 3),
                        "slo_exceeded": duration_ms > self._p95_target_ms,
                    },
                )

            async def observed_send(message: dict[str, Any]) -> None:
                nonlocal first_byte_recorded, status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    response_headers = list(message.get("headers", ()))
                    response_headers.append((b"x-request-id", request_id.encode("ascii")))
                    message = {**message, "headers": response_headers}
                await send(message)
                if message["type"] == "http.response.start" and not first_byte_recorded:
                    first_byte_recorded = True
                    self._first_byte.record(
                        (perf_counter() - started) * 1_000,
                        {
                            "http.request.method": method,
                            "http.route": route_template(),
                        },
                    )
                elif message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    record_completion()

            try:
                await self.app(scope, receive, observed_send)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                record_completion()


def install_request_observability(app: FastAPI, *, p95_target_ms: int) -> None:
    app.add_middleware(_RequestObservabilityMiddleware, p95_target_ms=p95_target_ms)


def instrument_sqlalchemy_engine(engine: Engine) -> None:
    """Trace SQL duration while deliberately omitting SQL text and bind values."""

    if getattr(engine, "_financeclaw_otel_instrumented", False):
        return
    engine._financeclaw_otel_instrumented = True

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany):
        operation = statement.lstrip().partition(" ")[0].upper()[:16] or "UNKNOWN"
        span = trace.get_tracer("financeclaw.database").start_span(f"db.sql.{operation.lower()}")
        span.set_attribute("db.system.name", engine.dialect.name)
        span.set_attribute("db.operation.name", operation)
        conn.info.setdefault("financeclaw_otel_spans", []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, _cursor, _statement, _parameters, _context, _executemany):
        spans = conn.info.get("financeclaw_otel_spans", [])
        if spans:
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def handle_error(exception_context):
        connection = exception_context.connection
        if connection is None:
            return
        spans = connection.info.get("financeclaw_otel_spans", [])
        if spans:
            span = spans.pop()
            span.record_exception(exception_context.original_exception)
            span.set_status(Status(StatusCode.ERROR))
            span.end()
