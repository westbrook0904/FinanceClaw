"""Direct Invocation 与计划执行共用的 Context/Trace 生命周期辅助能力。"""

from __future__ import annotations

from collections.abc import Mapping

from harness_contracts import (
    ErrorDetail,
    HarnessError,
    InvocationContext,
    JsonValue,
    Request,
    RequestError,
    ResultEnvelope,
    ResultStatus,
    TraceContext,
)
from harness_trace import Span, SpanStatus, SpanType, TraceError, Tracer

from .context import DefaultInvocationContextFactory, InvocationContextFactory


class InvocationLifecycle:
    """集中管理可被 Runtime 与 ExecutionEngine 复用的调用生命周期操作。"""

    def __init__(
        self,
        tracer: Tracer,
        *,
        context_factory: InvocationContextFactory | None = None,
    ) -> None:
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        effective_factory = context_factory or DefaultInvocationContextFactory()
        if not isinstance(effective_factory, InvocationContextFactory):
            raise TypeError("context_factory must implement InvocationContextFactory")
        self._tracer = tracer
        self._context_factory = effective_factory

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def context_factory(self) -> InvocationContextFactory:
        return self._context_factory

    def create_context(self, request: Request) -> InvocationContext | ResultEnvelope:
        """创建可信 Context，并把构造失败归一化为 Request 类结果。"""

        try:
            context = self._context_factory.create(request)
        except HarnessError as exc:
            return ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            error = RequestError(
                "failed to create invocation context",
                code="HARNESS.REQUEST.CONTEXT_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            return ResultEnvelope.failure(error.to_detail())

        if not isinstance(context, InvocationContext):
            error = RequestError(
                "context factory must return InvocationContext",
                code="HARNESS.REQUEST.INVALID_CONTEXT",
            )
            return ResultEnvelope.failure(error.to_detail())
        if context.request != request:
            error = RequestError(
                "context factory returned a context for another request",
                code="HARNESS.REQUEST.CONTEXT_MISMATCH",
            )
            return ResultEnvelope.failure(error.to_detail())
        return context

    def start_request_span(self, context: InvocationContext) -> Span:
        """为 Request 开始根 Span；同时兼容无 target 的计划请求。"""

        request = context.request
        target = request.target
        return self._tracer.start_span(
            f"request.{request.request_id}",
            SpanType.REQUEST,
            parent=context.trace_context,
            attributes=_compact_attributes(
                {
                    "request_id": request.request_id,
                    "session_id": request.session_id,
                    "target_capability": target.capability if target is not None else None,
                    "target_plugin": target.plugin if target is not None else None,
                }
            ),
        )

    def with_trace_context(self, context: InvocationContext, span: Span) -> InvocationContext:
        """返回传播当前 Span 的 Context 副本，不修改原始只读 Context。"""

        return context.model_copy(update={"trace_context": self._tracer.trace_context(span)})

    def normalize_trace_id(
        self,
        result: ResultEnvelope,
        source: Span | TraceContext | None,
    ) -> ResultEnvelope:
        """让结果使用当前受控调用链的 trace_id。"""

        if source is None:
            return result
        return ResultEnvelope.model_validate(
            {
                **result.model_dump(mode="json"),
                "trace_id": source.trace_id,
            }
        )

    def finish_from_result(
        self,
        span: Span | None,
        result: ResultEnvelope,
        *,
        error: BaseException | None = None,
    ) -> None:
        """根据统一结果语义关闭 Span。"""

        if span is None:
            return
        attributes = {"result_status": result.status.value}
        if result.status is ResultStatus.FAILED:
            trace_error = error or _trace_error_from_detail(result.error)
            self._tracer.end_span(
                span,
                status=SpanStatus.ERROR,
                error=trace_error,
                attributes=attributes,
            )
            return
        if result.status is ResultStatus.CANCELLED:
            self._tracer.end_span(
                span,
                status=SpanStatus.CANCELLED,
                attributes=attributes,
            )
            return
        self._tracer.end_span(span, status=SpanStatus.OK, attributes=attributes)

    def finish_ok(
        self,
        span: Span | None,
        *,
        attributes: dict[str, JsonValue] | None = None,
    ) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.OK, attributes=attributes)

    def finish_error(self, span: Span | None, error: BaseException) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.ERROR, error=error)

    def finish_cancelled(self, span: Span | None) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.CANCELLED)


def _trace_error_from_detail(detail: ErrorDetail | None) -> TraceError:
    if detail is None:
        return TraceError(type="ResultError", message="capability returned failed result")
    return TraceError(type=detail.category.value, message=detail.message, code=detail.code)


def _compact_attributes(values: Mapping[str, JsonValue | None]) -> dict[str, JsonValue]:
    return {key: value for key, value in values.items() if value is not None}
