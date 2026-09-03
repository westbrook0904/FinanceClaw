"""OpenTelemetry 遥测：全局 Provider 装配、BFF 请求观测中间件与 SQLAlchemy 插桩。

本模块属于 infrastructure 层的观测适配：把 HTTP 请求、数据库调用等
统一纳入链路与指标，支撑 p95 延迟 SLO 判定与优雅停机时的数据落盘。
"""

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

# 模块级 logger：用于输出请求完成的结构化访问日志。
LOGGER = logging.getLogger(__name__)
# 合法 request_id 的约束：1~128 位，仅允许字母、数字与 . _ : -。
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(slots=True)
class TelemetryRuntime:
    """遥测运行时句柄：持有已注册的 Provider 引用，用于优雅停机。

    使用场景：``configure_telemetry`` 的返回值由组合根保存，停机时调用
    ``shutdown()`` 冲刷未导出的 span 与指标；未启用遥测时两个字段为 None。

    Attributes:
        trace_provider: 全局链路 Provider；未启用链路导出时为 None。
        meter_provider: 全局指标 Provider；未配置指标端点时为 None。

    """

    trace_provider: TracerProvider | None
    meter_provider: MeterProvider | None

    def shutdown(self) -> None:
        """冲刷并关闭遥测 Provider：先停指标导出，再停链路导出。"""
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
    """构建并注册全局 Tracer/Meter Provider，返回用于停机的运行时句柄。

    使用场景：bootstrap.py 组合根启动时调用；``endpoint`` 未配置时返回
    空运行时（开发与测试无需上报），生产环境则双端点均必填。

    Args:
        service_name: 遥测资源中的服务名。
        environment: 部署环境名，写入资源属性。
        endpoint: 链路（trace）的 OTLP HTTP 上报端点；None 表示不启用。
        metrics_endpoint: 指标的 OTLP HTTP 上报端点；None 表示不启用指标。
        sample_rate: 链路采样率 [0, 1]，按父级采样策略生效。

    Returns:
        持有已注册 Provider 的运行时句柄。

    """
    # 1. 未配置链路端点时完全不启用遥测。
    if endpoint is None:
        return TelemetryRuntime(trace_provider=None, meter_provider=None)
    # 2. 构造资源属性：服务名、版本与部署环境。
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment.name": environment,
        }
    )
    # 3. 链路 Provider：父级采样策略 + 批处理 OTLP 导出，并注册为全局。
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    # 4. 指标 Provider 可选：配置了指标端点才启用周期导出。
    meter_provider = None
    if metrics_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=metrics_endpoint))
        meter_provider = MeterProvider(resource=resource, metric_readers=(reader,))
        metrics.set_meter_provider(meter_provider)
    return TelemetryRuntime(trace_provider=provider, meter_provider=meter_provider)


class _RequestObservabilityMiddleware:
    """纯 ASGI 请求观测中间件：生成请求 ID、记录延迟指标与访问日志。

    使用场景：``install_request_observability`` 挂载到 FastAPI；不基于
    ``BaseHTTPMiddleware``，避免额外任务包装对流式响应的影响。

    Attributes:
        app: 下游 ASGI 应用。
        _tracer: HTTP 层 tracer，为每个请求创建 SERVER span。
        _first_byte: 首字节延迟直方图（毫秒）。
        _completion: 请求完成时长直方图（毫秒）。
        _p95_target_ms: p95 延迟 SLO 目标（毫秒），写入完成日志用于判定。

    """

    def __init__(self, app: Any, *, p95_target_ms: int) -> None:
        """初始化 tracer 与指标直方图。

        Args:
            app: 下游 ASGI 应用。
            p95_target_ms: API p95 延迟 SLO 目标（毫秒）。

        """
        self.app = app
        self._tracer = trace.get_tracer("financeclaw.interfaces.http")
        meter = metrics.get_meter("financeclaw.interfaces.http")
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
        """处理一次 ASGI 调用：透传非 HTTP 请求，观测 HTTP 请求全生命周期。

        Args:
            scope: ASGI 作用域字典。
            receive: 上行消息接收函数。
            send: 下行消息发送函数。

        """
        # 1. 非 HTTP 协议（如 websocket/lifespan）不做请求观测，直接透传。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # 2. 解析请求头，优先复用合法的客户端 request_id，否则生成新 ID。
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
        # 3. 在 SERVER span 内执行下游应用，记录方法与请求 ID 属性。
        with self._tracer.start_as_current_span(span_name, kind=trace.SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("financeclaw.request_id", request_id)

            def route_template() -> str:
                """读取已匹配的路由模板（如 ``/conversations/{id}``），未匹配时回退。"""
                route = scope.get("route")
                return getattr(route, "path", "unmatched")

            def record_completion() -> None:
                """记录完成时长指标与访问日志；保证整个请求只执行一次。"""
                nonlocal completion_recorded
                if completion_recorded:
                    return
                completion_recorded = True
                duration_ms = (perf_counter() - started) * 1_000
                route = route_template()
                # 用具体路由模板重命名 span，避免高基数的 span 名称。
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
                """包装下游 send：注入 x-request-id 响应头，并记录首字节与完成时点。"""
                nonlocal first_byte_recorded, status_code
                # 首个响应消息：捕获状态码并把请求 ID 附加到响应头。
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    response_headers = list(message.get("headers", ()))
                    response_headers.append((b"x-request-id", request_id.encode("ascii")))
                    message = {**message, "headers": response_headers}
                await send(message)
                # 首次可发送响应时记录首字节延迟。
                if message["type"] == "http.response.start" and not first_byte_recorded:
                    first_byte_recorded = True
                    self._first_byte.record(
                        (perf_counter() - started) * 1_000,
                        {
                            "http.request.method": method,
                            "http.route": route_template(),
                        },
                    )
                # 响应体发送完毕（无更多分片）时记录完成指标与日志。
                elif message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    record_completion()

            try:
                # 4. 执行下游应用，异常时记录到 span 并标记 ERROR 后原样抛出。
                await self.app(scope, receive, observed_send)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                # 5. 兜底记录完成指标，覆盖下游未发送完整响应体的路径。
                record_completion()


def install_request_observability(app: FastAPI, *, p95_target_ms: int) -> None:
    """把请求观测中间件挂载到 FastAPI 应用。

    Args:
        app: 目标 FastAPI 应用。
        p95_target_ms: API p95 延迟 SLO 目标（毫秒）。

    """
    app.add_middleware(_RequestObservabilityMiddleware, p95_target_ms=p95_target_ms)


def instrument_sqlalchemy_engine(engine: Engine) -> None:
    """为 SQLAlchemy 引擎注入 OTel SQL 插桩：每条语句对应一个 span。

    使用场景：``ApplicationDatabase`` 创建引擎后调用；span 名称按 SQL
    首个关键字（如 select/insert）归类，异常时记录并标记 ERROR。

    Args:
        engine: 待插桩的 SQLAlchemy 引擎；重复插桩会被幂等跳过。

    """
    # 幂等保护：避免同一引擎被重复注册事件监听。
    if getattr(engine, "_financeclaw_otel_instrumented", False):
        return
    engine._financeclaw_otel_instrumented = True

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany):
        """语句执行前：按首个关键字归类操作并创建 span，挂到连接上下文。"""
        operation = statement.lstrip().partition(" ")[0].upper()[:16] or "UNKNOWN"
        span = trace.get_tracer("financeclaw.database").start_span(f"db.sql.{operation.lower()}")
        span.set_attribute("db.system.name", engine.dialect.name)
        span.set_attribute("db.operation.name", operation)
        conn.info.setdefault("financeclaw_otel_spans", []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, _cursor, _statement, _parameters, _context, _executemany):
        """语句执行成功：结束栈顶 span，保证嵌套调用按序配对。"""
        spans = conn.info.get("financeclaw_otel_spans", [])
        if spans:
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def handle_error(exception_context):
        """语句执行出错：结束栈顶 span 并记录异常、标记 ERROR。"""
        connection = exception_context.connection
        if connection is None:
            return
        spans = connection.info.get("financeclaw_otel_spans", [])
        if spans:
            span = spans.pop()
            span.record_exception(exception_context.original_exception)
            span.set_status(Status(StatusCode.ERROR))
            span.end()
