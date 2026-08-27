"""共享 Registry/Selection/Fallback 的非流式 ModelGateway。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityType,
    HarnessError,
    HarnessTimeoutError,
    InvocationContext,
    JsonValue,
    ProviderAttempt,
    ProviderError,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SelectionContext,
    TraceContext,
)
from harness_events import (
    EventPublisher,
    ExecutionEvent,
    ExecutionEventName,
    NoOpEventPublisher,
)
from harness_registry import CapabilityRegistry, ProviderRegistration
from harness_runtime import InvocationLifecycle, ProviderExecutionCoordinator, SelectedProvider
from harness_selection import PrioritySelector, ProviderSelector
from harness_trace import Span, SpanType, Tracer

from .contracts import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
)
from .provider import ModelProvider

type ModelInvocationParent = Span | TraceContext | None


class ModelGateway:
    """模型原生协议到共享 Provider Fabric 的受控入口。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        tracer: Tracer,
        *,
        lifecycle: InvocationLifecycle | None = None,
        provider_selector: ProviderSelector | None = None,
        provider_execution: ProviderExecutionCoordinator | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        effective_selector = provider_selector or (
            provider_execution.selector
            if isinstance(provider_execution, ProviderExecutionCoordinator)
            else PrioritySelector()
        )
        if not isinstance(effective_selector, ProviderSelector):
            raise TypeError("provider_selector must implement ProviderSelector")
        effective_execution = provider_execution or ProviderExecutionCoordinator(effective_selector)
        if not isinstance(effective_execution, ProviderExecutionCoordinator):
            raise TypeError("provider_execution must be ProviderExecutionCoordinator")
        if effective_execution.selector is not effective_selector:
            raise ValueError("provider_execution and gateway must use the same selector")
        effective_lifecycle = lifecycle or InvocationLifecycle(tracer)
        if not isinstance(effective_lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if effective_lifecycle.tracer is not tracer:
            raise ValueError("lifecycle and gateway must use the same tracer")
        effective_events = event_publisher or NoOpEventPublisher()
        if not isinstance(effective_events, EventPublisher):
            raise TypeError("event_publisher must implement EventPublisher")

        self._registry = registry
        self._tracer = tracer
        self._lifecycle = effective_lifecycle
        self._provider_selector = effective_selector
        self._provider_execution = effective_execution
        self._event_publisher = effective_events

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    @property
    def provider_selector(self) -> ProviderSelector:
        return self._provider_selector

    @property
    def provider_execution(self) -> ProviderExecutionCoordinator:
        return self._provider_execution

    @property
    def lifecycle(self) -> InvocationLifecycle:
        return self._lifecycle

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def event_publisher(self) -> EventPublisher:
        return self._event_publisher

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        retry_policy: RetryPolicy | None = None,
        timeout_ms: int | None = None,
        deadline_at: datetime | None = None,
        parent: ModelInvocationParent = None,
        trace_enabled: bool = True,
    ) -> GenerateResult:
        """选择 ModelProvider 并执行 timeout、same-provider retry 与 fallback。"""

        self._validate_generate(
            request,
            context,
            retry_policy=retry_policy,
            timeout_ms=timeout_ms,
            deadline_at=deadline_at,
            parent=parent,
        )
        trace = _TraceAnchor(parent if parent is not None else context.trace_context)
        effective_deadline = self._effective_deadline(context, deadline_at)

        try:
            candidates, selection_context, selected = self._resolve_model(
                request,
                context,
                deadline_at=effective_deadline,
                trace=trace,
                trace_enabled=trace_enabled,
            )
            current_attempt: ProviderAttempt | None = None
            last_provider_id: str | None = selected.registration.provider_id

            async def attempt_started(attempt: ProviderAttempt) -> None:
                nonlocal current_attempt
                current_attempt = attempt

            async def invoke_selected(target: SelectedProvider) -> ResultEnvelope:
                nonlocal last_provider_id
                last_provider_id = target.registration.provider_id
                return await self._invoke_selected(
                    request,
                    context,
                    target,
                    current_attempt,
                    timeout_ms=timeout_ms,
                    deadline_at=effective_deadline,
                    trace=trace,
                    trace_enabled=trace_enabled,
                )

            async def provider_event(name: str, attributes: dict[str, JsonValue]) -> None:
                await self._observe_provider_event(
                    name,
                    attributes,
                    context=context,
                    trace=trace,
                    trace_enabled=trace_enabled,
                )

            envelope = await self._provider_execution.execute(
                candidates,
                selection_context,
                invoke_selected,
                retry_policy=retry_policy,
                deadline_at=effective_deadline,
                initial_selection=selected,
                attempt_started=attempt_started,
                provider_event=provider_event,
            )
            result = self._from_envelope(envelope, provider_id=last_provider_id)
        except asyncio.CancelledError:
            raise
        except HarnessError as exc:
            result = GenerateResult.failure(exc.to_detail())
        except Exception as exc:
            wrapped = ProviderError(
                "model gateway generation failed",
                code="HARNESS.MODEL.GENERATION_FAILED",
                details={"model": request.model, "cause_type": type(exc).__name__},
            )
            result = GenerateResult.failure(wrapped.to_detail())

        return result.model_copy(
            update={
                "trace_id": (
                    trace.parent.trace_id
                    if trace_enabled and isinstance(trace.parent, Span | TraceContext)
                    else None
                )
            }
        )

    def _resolve_model(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        deadline_at: datetime | None,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> tuple[tuple[ProviderRegistration, ...], SelectionContext, SelectedProvider]:
        registry_span = (
            self._tracer.start_span(
                "registry.resolve_model",
                SpanType.REGISTRY_RESOLVE,
                parent=trace.parent,
                attributes={"capability_id": request.model},
            )
            if trace_enabled
            else None
        )
        trace.capture(registry_span)
        try:
            candidates = self._registry.candidates(request.model)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(registry_span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(registry_span, exc)
            raise
        except Exception as exc:
            wrapped = ProviderError(
                "model provider discovery failed",
                code="HARNESS.MODEL.REGISTRY_FAILED",
                details={"model": request.model, "cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(registry_span, wrapped)
            raise wrapped from exc

        if not candidates:
            error = ProviderError(
                "no model provider is registered",
                code="HARNESS.MODEL.NOT_FOUND",
                details={"model": request.model},
            )
            self._lifecycle.finish_error(registry_span, error)
            raise error
        for candidate in candidates:
            if candidate.capability.type is not CapabilityType.MODEL or not isinstance(
                candidate.provider, ModelProvider
            ):
                error = ProviderError(
                    "model candidate does not implement ModelProvider",
                    code="HARNESS.MODEL.INVALID_PROVIDER",
                    details={
                        "model": request.model,
                        "provider_id": candidate.provider_id,
                        "capability_type": candidate.capability.type.value,
                    },
                )
                self._lifecycle.finish_error(registry_span, error)
                raise error

        capability = candidates[0].capability
        selection_context = SelectionContext(
            request_id=context.request.request_id,
            capability_id=request.model,
            tenant_id=(context.tenant.tenant_id if context.tenant is not None else None),
            identity_subject=(context.identity.subject if context.identity is not None else None),
            side_effect=capability.execution_profile.side_effect,
            egress=capability.execution_profile.egress,
            deadline_at=deadline_at,
        )
        selection_span = (
            self._tracer.start_span(
                "provider.select_model",
                SpanType.PROVIDER_SELECT,
                parent=trace.parent,
                attributes={
                    "capability_id": request.model,
                    "candidate_count": len(candidates),
                    "selector": self._provider_selector.name,
                    "provider_attempt": 1,
                    "retry_attempt": 1,
                },
            )
            if trace_enabled
            else None
        )
        trace.capture(selection_span)
        try:
            selected = self._provider_execution.select(candidates, selection_context)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(selection_span)
            self._lifecycle.finish_cancelled(registry_span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(selection_span, exc)
            self._lifecycle.finish_error(registry_span, exc)
            raise
        except Exception as exc:
            wrapped = ProviderError(
                "model provider selection failed",
                code="HARNESS.MODEL.SELECTION_FAILED",
                details={"model": request.model, "cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(selection_span, wrapped)
            self._lifecycle.finish_error(registry_span, wrapped)
            raise wrapped from exc

        self._lifecycle.finish_ok(
            selection_span,
            attributes={
                "provider_id": selected.registration.provider_id,
                "selection_key": selected.decision.selection_key,
                "selection_reason": selected.decision.reason_code,
            },
        )
        self._lifecycle.finish_ok(
            registry_span,
            attributes={
                "provider_id": selected.registration.provider_id,
                "candidate_count": len(candidates),
                "selection_key": selected.decision.selection_key,
            },
        )
        return candidates, selection_context, selected

    async def _invoke_selected(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        selected: SelectedProvider,
        attempt: ProviderAttempt | None,
        *,
        timeout_ms: int | None,
        deadline_at: datetime | None,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        provider = selected.registration.provider
        if not isinstance(provider, ModelProvider):
            raise ProviderError(
                "selected provider does not implement ModelProvider",
                code="HARNESS.MODEL.INVALID_PROVIDER",
                fallbackable=True,
            )
        model_span = (
            self._tracer.start_span(
                f"model.{request.model}",
                SpanType.MODEL,
                parent=trace.parent,
                attributes={
                    "model": request.model,
                    "provider_id": selected.registration.provider_id,
                    "provider_attempt": attempt.provider_attempt if attempt is not None else 1,
                    "retry_attempt": attempt.retry_attempt if attempt is not None else 1,
                    "selection_key": selected.decision.selection_key,
                },
            )
            if trace_enabled
            else None
        )
        execution_context = (
            self._lifecycle.with_trace_context(context, model_span)
            if model_span is not None
            else context
        )
        try:
            result = await self._call_model(
                provider,
                request,
                execution_context,
                timeout_ms=timeout_ms,
                deadline_at=deadline_at,
            )
            if not isinstance(result, GenerateResult):
                raise ProviderError(
                    "model provider must return GenerateResult",
                    code="HARNESS.MODEL.INVALID_RESULT",
                    retryable=True,
                    fallbackable=True,
                )
            result = result.model_copy(update={"provider_id": selected.registration.provider_id})
            self._validate_structured_output(request, result)
            envelope = self._to_envelope(result)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(model_span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(model_span, exc)
            raise
        except Exception as exc:
            wrapped = ProviderError(
                "model provider execution failed",
                code="HARNESS.MODEL.GENERATION_FAILED",
                details={
                    "model": request.model,
                    "provider_id": selected.registration.provider_id,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
                fallbackable=True,
            )
            self._lifecycle.finish_error(model_span, wrapped)
            raise wrapped from exc

        self._lifecycle.finish_from_result(model_span, envelope)
        return envelope

    async def _call_model(
        self,
        provider: ModelProvider,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        timeout_ms: int | None,
        deadline_at: datetime | None,
    ) -> GenerateResult:
        limits: list[float] = []
        if timeout_ms is not None:
            limits.append(timeout_ms / 1000)
        if deadline_at is not None:
            limits.append((deadline_at - datetime.now(UTC)).total_seconds())
        if not limits:
            return await provider.generate(request, context)
        remaining = min(limits)
        if remaining <= 0:
            raise HarnessTimeoutError(
                "model generation deadline exceeded",
                details={"model": request.model},
                fallbackable=True,
            )
        try:
            async with asyncio.timeout(remaining):
                return await provider.generate(request, context)
        except TimeoutError as exc:
            raise HarnessTimeoutError(
                "model provider timed out",
                details={"model": request.model, "timeout_ms": timeout_ms},
                fallbackable=True,
            ) from exc

    @staticmethod
    def _validate_structured_output(
        request: GenerateRequest,
        result: GenerateResult,
    ) -> None:
        if result.status is GenerateStatus.FAILED:
            return
        if result.output is None or result.output.type is not request.response_format:
            raise ProviderError(
                "model response format does not match request",
                code="HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID",
                retryable=True,
                fallbackable=True,
            )
        schema = request.response_schema
        if schema is None:
            return
        data = result.output.data
        schema_type = schema.get("type")
        if schema_type == "object" and not isinstance(data, Mapping):
            raise ProviderError(
                "structured output must be an object",
                code="HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID",
                retryable=True,
                fallbackable=True,
            )
        if schema_type == "array" and not isinstance(data, tuple | list):
            raise ProviderError(
                "structured output must be an array",
                code="HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID",
                retryable=True,
                fallbackable=True,
            )
        required = schema.get("required")
        if isinstance(required, tuple | list) and isinstance(data, Mapping):
            missing = [item for item in required if isinstance(item, str) and item not in data]
            if missing:
                raise ProviderError(
                    "structured output is missing required properties",
                    code="HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID",
                    details={"missing": missing},
                    retryable=True,
                    fallbackable=True,
                )

    async def _observe_provider_event(
        self,
        name: str,
        attributes: dict[str, JsonValue],
        *,
        context: InvocationContext,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> None:
        compact = {key: value for key, value in attributes.items() if value is not None}
        if trace_enabled:
            try:
                if isinstance(trace.parent, Span):
                    self._tracer.add_event(trace.parent, name, attributes=compact)
                if (
                    name == ExecutionEventName.PROVIDER_SELECTED.value
                    and compact.get("phase") == "fallback"
                ):
                    selection_span = self._tracer.start_span(
                        "provider.select_model",
                        SpanType.PROVIDER_SELECT,
                        parent=trace.parent,
                        attributes=compact,
                    )
                    trace.capture(selection_span)
                    self._lifecycle.finish_ok(selection_span)
            except Exception:
                pass

        plan_id = context.attributes.get("plan_id")
        node_id = context.attributes.get("node_id")
        try:
            await self._event_publisher.publish(
                ExecutionEvent(
                    name=ExecutionEventName(name),
                    request_id=context.request.request_id,
                    plan_id=plan_id if isinstance(plan_id, str) and plan_id.strip() else None,
                    node_id=node_id if isinstance(node_id, str) and node_id.strip() else None,
                    trace_id=(
                        trace.parent.trace_id
                        if isinstance(trace.parent, Span | TraceContext)
                        else None
                    ),
                    attributes=compact,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _to_envelope(result: GenerateResult) -> ResultEnvelope:
        if result.status is GenerateStatus.SUCCESS:
            return ResultEnvelope.success(
                ResultOutput(
                    type="model.generate_result",
                    data=result.model_dump(mode="json"),
                )
            )
        if result.error is None:
            error = ProviderError(
                "failed model result is missing an error",
                code="HARNESS.MODEL.INVALID_RESULT",
            )
            return ResultEnvelope.failure(error.to_detail())
        return ResultEnvelope.failure(result.error)

    @staticmethod
    def _from_envelope(
        envelope: ResultEnvelope,
        *,
        provider_id: str | None,
    ) -> GenerateResult:
        if envelope.status is ResultStatus.SUCCESS and envelope.output is not None:
            if envelope.output.type != "model.generate_result":
                error = ProviderError(
                    "model coordinator returned an invalid success envelope",
                    code="HARNESS.MODEL.INVALID_RESULT",
                )
                return GenerateResult.failure(error.to_detail(), provider_id=provider_id)
            return GenerateResult.model_validate(envelope.output.data)
        error = (
            envelope.error
            or ProviderError(
                "model coordinator returned a non-final result",
                code="HARNESS.MODEL.INVALID_RESULT",
            ).to_detail()
        )
        return GenerateResult.failure(error, provider_id=provider_id)

    @staticmethod
    def _effective_deadline(
        context: InvocationContext,
        deadline_at: datetime | None,
    ) -> datetime | None:
        values = [item for item in (context.deadline_at, deadline_at) if item is not None]
        return min(values) if values else None

    @staticmethod
    def _validate_generate(
        request: GenerateRequest,
        context: InvocationContext,
        *,
        retry_policy: RetryPolicy | None,
        timeout_ms: int | None,
        deadline_at: datetime | None,
        parent: ModelInvocationParent,
    ) -> None:
        if not isinstance(request, GenerateRequest):
            raise TypeError("request must be GenerateRequest")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be RetryPolicy when provided")
        if timeout_ms is not None and (
            not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0
        ):
            raise TypeError("timeout_ms must be a positive integer when provided")
        if deadline_at is not None and (
            not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() is None
        ):
            raise TypeError("deadline_at must be timezone-aware when provided")
        if parent is not None and not isinstance(parent, Span | TraceContext):
            raise TypeError("parent must be Span, TraceContext, or None")


@dataclass(slots=True)
class _TraceAnchor:
    parent: ModelInvocationParent

    def capture(self, span: Span | None) -> None:
        if self.parent is None and span is not None:
            self.parent = TraceContext(trace_id=span.trace_id)
