"""阶段一 Harness Runtime 的 Invocation 编排。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from harness_contracts import (
    CapabilityError,
    ErrorDetail,
    HarnessError,
    HarnessTimeoutError,
    InvocationContext,
    JsonValue,
    PolicyError,
    RegistryError,
    Request,
    RequestError,
    ResultEnvelope,
    ResultStatus,
)
from harness_policy import (
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyPhase,
)
from harness_registry import CapabilityQuery, CapabilityRegistry, ResolvedCapability
from harness_spi import AgentRequest, AgentSPI, ToolRequest, ToolSPI
from harness_trace import Span, SpanStatus, SpanType, TraceError, Tracer

from .context import DefaultInvocationContextFactory, InvocationContextFactory


class HarnessRuntime:
    """协调一次 Request 的阶段一 Invocation 生命周期。

    Runtime 只负责 Context、Trace、Registry、Policy 和 Capability SPI 之间的编排，
    不包含 Planner、业务规则、数据访问或具体 Agent/Tool 实现。
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_engine: PolicyEngine,
        tracer: Tracer,
        *,
        context_factory: InvocationContextFactory | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")

        self._registry = registry
        self._policy_engine = policy_engine
        self._tracer = tracer
        self._context_factory = context_factory or DefaultInvocationContextFactory()

    async def invoke(self, request: Request) -> ResultEnvelope:
        """执行一次完整 Invocation，并始终返回统一 ``ResultEnvelope``。

        调用方主动取消当前 task 时保留 ``CancelledError`` 语义，不把取消吞成普通失败。
        """

        context_result = self._create_context(request)
        if isinstance(context_result, ResultEnvelope):
            return context_result
        context = context_result

        trace_enabled = request.options.trace
        request_span = self._start_request_span(context) if trace_enabled else None
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
            context = self._with_trace_context(context, runtime_span)

        try:
            result = await self._invoke_pipeline(
                request,
                context,
                runtime_span=runtime_span,
                trace_enabled=trace_enabled,
            )
        except asyncio.CancelledError:
            self._finish_cancelled(runtime_span)
            self._finish_cancelled(request_span)
            raise
        except HarnessError as exc:
            result = ResultEnvelope.failure(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            self._finish_from_result(runtime_span, result, error=exc)
            self._finish_from_result(request_span, result, error=exc)
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
            self._finish_from_result(runtime_span, result, error=wrapped)
            self._finish_from_result(request_span, result, error=wrapped)
            return result

        result = self._normalize_trace_id(result, request_span)
        self._finish_from_result(runtime_span, result)
        self._finish_from_result(request_span, result)
        return result

    def _create_context(self, request: Request) -> InvocationContext | ResultEnvelope:
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

    async def _invoke_pipeline(
        self,
        request: Request,
        context: InvocationContext,
        *,
        runtime_span: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        resolved = self._resolve_capability(
            request,
            parent=runtime_span,
            trace_enabled=trace_enabled,
        )
        decision = self._evaluate_policy(
            context,
            resolved,
            parent=runtime_span,
            trace_enabled=trace_enabled,
        )
        if decision.effect is PolicyEffect.DENY:
            constraints = decision.model_dump(mode="json")["constraints"]
            error = PolicyError(
                decision.reason or "policy denied invocation",
                details={"policy": decision.policy, "constraints": constraints},
            )
            return ResultEnvelope.denied(error.to_detail())

        return await self._invoke_capability(
            request,
            context,
            resolved,
            parent=runtime_span,
            trace_enabled=trace_enabled,
        )

    def _resolve_capability(
        self,
        request: Request,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResolvedCapability:
        span = (
            self._tracer.start_span(
                "registry.resolve",
                SpanType.REGISTRY_RESOLVE,
                parent=parent,
                attributes=_compact_attributes(
                    {
                        "capability_id": request.target.capability,
                        "plugin_id": request.target.plugin,
                    }
                ),
            )
            if trace_enabled
            else None
        )
        try:
            resolved = self._registry.resolve(
                CapabilityQuery(
                    id=request.target.capability,
                    plugin_id=request.target.plugin,
                )
            )
        except asyncio.CancelledError:
            self._finish_cancelled(span)
            raise
        except RegistryError as exc:
            self._finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = RegistryError(
                "capability resolution failed",
                code="HARNESS.REGISTRY.RESOLVE_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._finish_error(span, wrapped)
            raise wrapped from exc

        self._finish_ok(
            span,
            attributes={
                "resolved_capability_id": resolved.descriptor.id,
                "plugin_id": resolved.plugin_id,
                "capability_type": resolved.descriptor.type.value,
            },
        )
        return resolved

    def _evaluate_policy(
        self,
        context: InvocationContext,
        resolved: ResolvedCapability,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> PolicyDecision:
        span = (
            self._tracer.start_span(
                "policy.pre_execute",
                SpanType.POLICY,
                parent=parent,
                attributes={"capability_id": resolved.descriptor.id},
            )
            if trace_enabled
            else None
        )
        policy_context = PolicyContext(
            invocation=(self._with_trace_context(context, span) if span is not None else context),
            capability=resolved.descriptor,
            phase=PolicyPhase.PRE_EXECUTE,
        )

        try:
            decision = self._policy_engine.evaluate(policy_context)
        except asyncio.CancelledError:
            self._finish_cancelled(span)
            raise
        except PolicyError as exc:
            self._finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = PolicyError(
                "policy evaluation failed",
                code="HARNESS.POLICY.EVALUATION_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._finish_error(span, wrapped)
            raise wrapped from exc

        self._finish_ok(
            span,
            attributes={
                "effect": decision.effect.value,
                "policy": decision.policy,
            },
        )
        return decision

    async def _invoke_capability(
        self,
        request: Request,
        context: InvocationContext,
        resolved: ResolvedCapability,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        capability_span = (
            self._tracer.start_span(
                f"capability.{resolved.descriptor.id}",
                SpanType.CAPABILITY,
                parent=parent,
                attributes={
                    "capability_id": resolved.descriptor.id,
                    "plugin_id": resolved.plugin_id,
                },
            )
            if trace_enabled
            else None
        )

        provider = resolved.provider
        leaf_type: SpanType
        leaf_name: str
        if isinstance(provider, AgentSPI):
            leaf_type = SpanType.AGENT
            leaf_name = f"agent.{resolved.descriptor.id}"
        elif isinstance(provider, ToolSPI):
            leaf_type = SpanType.TOOL
            leaf_name = f"tool.{resolved.descriptor.id}"
        else:
            error = CapabilityError(
                "resolved provider is neither AgentSPI nor ToolSPI",
                code="HARNESS.CAPABILITY.INVALID_PROVIDER",
                details={"capability_id": resolved.descriptor.id},
            )
            self._finish_error(capability_span, error)
            raise error

        try:
            self._validate_provider_type(resolved, provider)
        except CapabilityError as exc:
            self._finish_error(capability_span, exc)
            raise

        leaf_span = (
            self._tracer.start_span(
                leaf_name,
                leaf_type,
                parent=capability_span,
                attributes={"capability_id": resolved.descriptor.id},
            )
            if trace_enabled
            else None
        )
        execution_context = (
            self._with_trace_context(context, leaf_span) if leaf_span is not None else context
        )

        try:
            result = await self._call_provider(
                request,
                execution_context,
                resolved,
                provider,
            )
        except asyncio.CancelledError:
            self._finish_cancelled(leaf_span)
            self._finish_cancelled(capability_span)
            raise
        except HarnessError as exc:
            self._finish_error(leaf_span, exc)
            self._finish_error(capability_span, exc)
            raise
        except Exception as exc:
            wrapped = CapabilityError(
                "capability execution failed",
                details={
                    "capability_id": resolved.descriptor.id,
                    "cause_type": type(exc).__name__,
                },
            )
            self._finish_error(leaf_span, wrapped)
            self._finish_error(capability_span, wrapped)
            raise wrapped from exc

        if not isinstance(result, ResultEnvelope):
            error = CapabilityError(
                "capability must return ResultEnvelope",
                code="HARNESS.CAPABILITY.INVALID_RESULT",
                details={"capability_id": resolved.descriptor.id},
            )
            self._finish_error(leaf_span, error)
            self._finish_error(capability_span, error)
            raise error

        self._finish_from_result(leaf_span, result)
        self._finish_from_result(capability_span, result)
        return result

    async def _call_provider(
        self,
        request: Request,
        context: InvocationContext,
        resolved: ResolvedCapability,
        provider: AgentSPI | ToolSPI,
    ) -> ResultEnvelope:
        async def execute() -> ResultEnvelope:
            if isinstance(provider, AgentSPI):
                return await provider.invoke(AgentRequest(input=request.input), context)

            payload = request.input.model_dump(mode="json")["content"]
            if not isinstance(payload, dict):
                raise RequestError(
                    "tool input content must be a JSON object",
                    details={"capability_id": resolved.descriptor.id},
                )
            return await provider.execute(ToolRequest(arguments=payload), context)

        timeout_ms = request.options.timeout_ms
        if timeout_ms is None:
            return await execute()

        try:
            async with asyncio.timeout(timeout_ms / 1000):
                return await execute()
        except TimeoutError as exc:
            raise HarnessTimeoutError(
                "capability execution timed out",
                details={
                    "capability_id": resolved.descriptor.id,
                    "timeout_ms": timeout_ms,
                },
            ) from exc

    def _validate_provider_type(
        self,
        resolved: ResolvedCapability,
        provider: AgentSPI | ToolSPI,
    ) -> None:
        expected = resolved.descriptor.type.value
        actual = "agent" if isinstance(provider, AgentSPI) else "tool"
        if expected != actual:
            raise CapabilityError(
                "provider type does not match capability descriptor",
                code="HARNESS.CAPABILITY.TYPE_MISMATCH",
                details={
                    "capability_id": resolved.descriptor.id,
                    "descriptor_type": expected,
                    "provider_type": actual,
                },
            )

    def _start_request_span(self, context: InvocationContext) -> Span:
        request = context.request
        return self._tracer.start_span(
            f"request.{request.request_id}",
            SpanType.REQUEST,
            parent=context.trace_context,
            attributes=_compact_attributes(
                {
                    "request_id": request.request_id,
                    "session_id": request.session_id,
                    "target_capability": request.target.capability,
                    "target_plugin": request.target.plugin,
                }
            ),
        )

    def _with_trace_context(self, context: InvocationContext, span: Span) -> InvocationContext:
        return context.model_copy(update={"trace_context": self._tracer.trace_context(span)})

    def _normalize_trace_id(
        self,
        result: ResultEnvelope,
        request_span: Span | None,
    ) -> ResultEnvelope:
        if request_span is None:
            return result
        return ResultEnvelope.model_validate(
            {
                **result.model_dump(mode="json"),
                "trace_id": request_span.trace_id,
            }
        )

    def _finish_from_result(
        self,
        span: Span | None,
        result: ResultEnvelope,
        *,
        error: BaseException | None = None,
    ) -> None:
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
        self._tracer.end_span(span, status=SpanStatus.OK, attributes=attributes)

    def _finish_ok(
        self,
        span: Span | None,
        *,
        attributes: dict[str, JsonValue] | None = None,
    ) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.OK, attributes=attributes)

    def _finish_error(self, span: Span | None, error: BaseException) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.ERROR, error=error)

    def _finish_cancelled(self, span: Span | None) -> None:
        if span is not None:
            self._tracer.end_span(span, status=SpanStatus.CANCELLED)


def _trace_error_from_detail(detail: ErrorDetail | None) -> TraceError:
    if detail is None:
        return TraceError(type="ResultError", message="capability returned failed result")
    return TraceError(type=detail.category.value, message=detail.message, code=detail.code)


def _compact_attributes(values: Mapping[str, JsonValue | None]) -> dict[str, JsonValue]:
    return {key: value for key, value in values.items() if value is not None}
