"""Direct Invocation 的 Request 级生命周期协调。"""

from __future__ import annotations

import asyncio

from harness_contracts import (
    CapabilityError,
    ErrorCode,
    HarnessError,
    Request,
    RequestError,
    ResultEnvelope,
)
from harness_policy import PolicyEngine
from harness_registry import CapabilityRegistry
from harness_trace import SpanType, Tracer

from .context import DefaultInvocationContextFactory, InvocationContextFactory
from .invoker import CapabilityInvoker
from .lifecycle import InvocationLifecycle


class HarnessRuntime:
    """协调 Direct Request 生命周期，并把能力调用委托给 CapabilityInvoker。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_engine: PolicyEngine,
        tracer: Tracer,
        *,
        context_factory: InvocationContextFactory | None = None,
        lifecycle: InvocationLifecycle | None = None,
        invoker: CapabilityInvoker | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if lifecycle is not None and context_factory is not None:
            raise ValueError("context_factory and lifecycle cannot both be provided")

        effective_lifecycle = lifecycle or InvocationLifecycle(
            tracer,
            context_factory=context_factory or DefaultInvocationContextFactory(),
        )
        if not isinstance(effective_lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if effective_lifecycle.tracer is not tracer:
            raise ValueError("lifecycle and runtime must use the same tracer")

        effective_invoker = invoker or CapabilityInvoker(
            registry,
            policy_engine,
            tracer,
            lifecycle=effective_lifecycle,
        )
        if not isinstance(effective_invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if effective_invoker.registry is not registry:
            raise ValueError("invoker and runtime must use the same registry")
        if effective_invoker.policy_engine is not policy_engine:
            raise ValueError("invoker and runtime must use the same policy_engine")
        if effective_invoker.tracer is not tracer:
            raise ValueError("invoker and runtime must use the same tracer")
        if effective_invoker.lifecycle is not effective_lifecycle:
            raise ValueError("invoker and runtime must use the same lifecycle")

        self._tracer = tracer
        self._lifecycle = effective_lifecycle
        self._invoker = effective_invoker

    @property
    def lifecycle(self) -> InvocationLifecycle:
        return self._lifecycle

    @property
    def invoker(self) -> CapabilityInvoker:
        return self._invoker

    async def invoke(self, request: Request) -> ResultEnvelope:
        """执行 Direct Invocation；调用方 task 取消时继续传播 CancelledError。"""

        context_result = self._lifecycle.create_context(request)
        if isinstance(context_result, ResultEnvelope):
            return context_result
        context = context_result

        trace_enabled = request.options.trace
        request_span = self._lifecycle.start_request_span(context) if trace_enabled else None
        runtime_span = (
            self._tracer.start_span(
                "runtime.invoke",
                SpanType.RUNTIME,
                parent=request_span,
                attributes={"request_id": request.request_id},
            )
            if trace_enabled
            else None
        )
        if runtime_span is not None:
            context = self._lifecycle.with_trace_context(context, runtime_span)

        try:
            target = request.target
            if target is None:
                raise RequestError(
                    "direct invocation requires request target",
                    code=ErrorCode.REQUEST_TARGET_REQUIRED,
                )
            result = await self._invoker.invoke(
                target.capability,
                request.input,
                context,
                plugin_id=target.plugin,
                timeout_ms=request.options.timeout_ms,
                deadline_at=context.deadline_at,
                parent=runtime_span,
                trace_enabled=trace_enabled,
            )
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise
        except HarnessError as exc:
            result = ResultEnvelope.failure(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            self._lifecycle.finish_from_result(runtime_span, result, error=exc)
            self._lifecycle.finish_from_result(request_span, result, error=exc)
            return result
        except Exception as exc:
            wrapped = CapabilityError(
                "runtime invocation failed",
                code="HARNESS.RUNTIME.FAILED",
                details={"cause_type": type(exc).__name__},
            )
            result = ResultEnvelope.failure(
                wrapped.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            self._lifecycle.finish_from_result(runtime_span, result, error=wrapped)
            self._lifecycle.finish_from_result(request_span, result, error=wrapped)
            return result

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result
