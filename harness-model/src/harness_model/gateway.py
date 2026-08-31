"""共享 Registry/Selection/Fallback 的非流式 ModelGateway。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

from harness_contracts import (
    CapabilityType,
    ErrorCode,
    HarnessError,
    HarnessTimeoutError,
    InvocationContext,
    JsonValue,
    ModelGenerationAttemptSlot,
    ModelGenerationReservation,
    ModelProviderAttemptUsage,
    ModelReservationReceipt,
    ModelSlotExecutionTicket,
    NormalizedCost,
    ProviderAttempt,
    ProviderError,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SelectionContext,
    SideEffectType,
    StructuredOutputSpec,
    StructuredOutputStrictness,
    TraceContext,
    UnsupportedStructuredOutputBehavior,
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
from harness_trace import Span, SpanType, TraceError, Tracer

from .accounting import ModelAccountingAccumulator
from .contracts import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelFinishReason,
)
from .preparation import (
    ModelAttemptPolicy,
    ModelGenerationCheckpointSink,
    PreparedModelGeneration,
    PreparedStructuredOutput,
)
from .provider import ModelProvider
from .schema import (
    SchemaValidationFailure,
    stable_request_fingerprint,
    structured_schema_hash,
    validate_schema_definition,
    validate_structured_value,
)

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
        generation_checkpoint_sink: ModelGenerationCheckpointSink | None = None,
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
        if generation_checkpoint_sink is not None and not isinstance(
            generation_checkpoint_sink, ModelGenerationCheckpointSink
        ):
            raise TypeError(
                "generation_checkpoint_sink must implement ModelGenerationCheckpointSink"
            )

        self._registry = registry
        self._tracer = tracer
        self._lifecycle = effective_lifecycle
        self._provider_selector = effective_selector
        self._provider_execution = effective_execution
        self._event_publisher = effective_events
        self._generation_checkpoint_sink = generation_checkpoint_sink

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
        accounting = ModelAccountingAccumulator()

        try:
            structured = self._prepare_structured_request(request)
            candidates, selection_context, selected, prepared_by_provider = self._resolve_model(
                request,
                context,
                deadline_at=effective_deadline,
                trace=trace,
                trace_enabled=trace_enabled,
                structured=structured,
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
                    structured=structured,
                    prepared=prepared_by_provider.get(target.registration.provider_id),
                    accounting=accounting,
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

        result = accounting.attach(result)

        return result.model_copy(
            update={
                "trace_id": (
                    trace.parent.trace_id
                    if trace_enabled and isinstance(trace.parent, Span | TraceContext)
                    else None
                )
            }
        )

    async def prepare_generation(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        attempt_policy: ModelAttemptPolicy | None = None,
    ) -> PreparedModelGeneration:
        """冻结 strict generation 的 Provider、attempt 与最坏资源上界；零 outbound。"""

        if not isinstance(request, GenerateRequest):
            raise TypeError("request must be GenerateRequest")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")
        policy = attempt_policy or ModelAttemptPolicy()
        if not isinstance(policy, ModelAttemptPolicy):
            raise TypeError("attempt_policy must be ModelAttemptPolicy when provided")
        if (
            request.structured_output is None
            or request.structured_output.strictness is not StructuredOutputStrictness.REQUIRED
        ):
            raise ProviderError(
                "budgeted generation requires strict structured output",
                code=ErrorCode.MODEL_RESERVATION_INVALID,
            )
        if request.max_output_tokens is None:
            raise ProviderError(
                "budgeted generation requires max_output_tokens",
                code=ErrorCode.MODEL_RESERVATION_INVALID,
            )

        structured = self._prepare_structured_request(request)
        if structured is None:
            raise AssertionError("strict request must produce structured state")
        candidates, selection_context, selected, prepared_by_provider = self._resolve_model(
            request,
            context,
            deadline_at=self._effective_deadline(context, None),
            trace=_TraceAnchor(None),
            trace_enabled=False,
            structured=structured,
        )
        ordered = self._ordered_reservation_candidates(
            candidates,
            selection_context,
            selected,
            max_count=policy.max_provider_count if policy.allow_fallback else 1,
        )

        slot_specs: list[tuple[ProviderRegistration, PreparedStructuredOutput, int, int]] = []
        cost_unit: str | None = None
        reserve_cost_bounds: bool | None = None
        for registration in ordered:
            provider = registration.provider
            prepared = prepared_by_provider.get(registration.provider_id)
            if not isinstance(provider, ModelProvider) or not isinstance(
                prepared, PreparedStructuredOutput
            ):
                continue
            if policy.require_complete_accounting and not registration.model_features.usage_tokens:
                continue
            try:
                input_bound = provider.bound_input_tokens(request, prepared)
            except Exception:
                input_bound = None
            if (
                not isinstance(input_bound, int)
                or isinstance(input_bound, bool)
                or input_bound < 0
                or (
                    policy.max_input_tokens_per_call is not None
                    and input_bound > policy.max_input_tokens_per_call
                )
            ):
                continue
            rate = registration.model_features.cost_rate
            if policy.require_cost_bounds and rate is None:
                continue
            has_cost_bound = rate is not None
            if reserve_cost_bounds is not None and has_cost_bound != reserve_cost_bounds:
                continue
            if reserve_cost_bounds is None:
                reserve_cost_bounds = has_cost_bound
            if rate is not None:
                if cost_unit is not None and rate.unit != cost_unit:
                    continue
                cost_unit = rate.unit
            for retry_attempt in range(1, policy.retry_policy.max_attempts + 1):
                slot_specs.append((registration, prepared, input_bound, retry_attempt))

        if not slot_specs:
            raise ProviderError(
                "no provider can produce sound generation resource bounds",
                code=ErrorCode.MODEL_RESERVATION_INVALID,
                details={"model": request.model, "schema_hash": structured.schema_hash},
            )

        generation_id = str(uuid4())
        slots: list[ModelGenerationAttemptSlot] = []
        prepared_by_slot: dict[str, PreparedStructuredOutput] = {}
        for ordinal, (registration, prepared, input_bound, _retry_attempt) in enumerate(
            slot_specs, start=1
        ):
            slot_id = f"{generation_id}:{ordinal}"
            rate = registration.model_features.cost_rate
            cost_bound = None
            if rate is not None:
                amount = (
                    input_bound * rate.max_input_token_cost
                    + request.max_output_tokens * rate.max_output_token_cost
                    + rate.max_request_cost
                )
                if not math.isfinite(amount):
                    raise ProviderError(
                        "provider cost upper bound is not finite",
                        code=ErrorCode.MODEL_RESERVATION_INVALID,
                    )
                cost_bound = NormalizedCost(unit=rate.unit, amount=amount)
            slot = ModelGenerationAttemptSlot(
                slot_id=slot_id,
                provider_id=registration.provider_id,
                provider_registration_version=registration.registration_version,
                provider_features_hash=registration.model_features_hash,
                prepared_schema_hash=structured.schema_hash,
                provider_attempt=ordinal,
                input_token_upper_bound=input_bound,
                output_token_upper_bound=request.max_output_tokens,
                token_upper_bound=input_bound + request.max_output_tokens,
                normalized_cost_upper_bound=cost_bound,
            )
            slots.append(slot)
            prepared_by_slot[slot_id] = prepared

        request_fingerprint = stable_request_fingerprint(request.model_dump(mode="json"))
        registry_snapshot_hash = stable_request_fingerprint(
            [
                {
                    "provider_id": registration.provider_id,
                    "registration_version": registration.registration_version,
                    "features_hash": registration.model_features_hash,
                }
                for registration in ordered
                if any(slot.provider_id == registration.provider_id for slot in slots)
            ]
        )
        total_cost = None
        cost_bounds = [
            slot.normalized_cost_upper_bound
            for slot in slots
            if slot.normalized_cost_upper_bound is not None
        ]
        if cost_bounds:
            total_cost = NormalizedCost(
                unit=cost_bounds[0].unit,
                amount=sum(item.amount for item in cost_bounds),
            )
        reservation_payload = {
            "generation_id": generation_id,
            "request_fingerprint": request_fingerprint,
            "schema_hash": structured.schema_hash,
            "registry_snapshot_hash": registry_snapshot_hash,
            "slots": [slot.model_dump(mode="json") for slot in slots],
            "total_token_upper_bound": sum(slot.token_upper_bound for slot in slots),
            "total_cost_upper_bound": (
                total_cost.model_dump(mode="json") if total_cost is not None else None
            ),
        }
        reservation = ModelGenerationReservation(
            **reservation_payload,
            reservation_hash=stable_request_fingerprint(reservation_payload),
        )
        used_provider_ids = {slot.provider_id for slot in slots}
        return PreparedModelGeneration(
            request=request,
            reservation=reservation,
            registrations=tuple(
                registration
                for registration in ordered
                if registration.provider_id in used_provider_ids
            ),
            prepared_by_slot=MappingProxyType(prepared_by_slot),
            attempt_policy=policy,
            process_nonce=str(uuid4()),
        )

    async def execute_prepared(
        self,
        prepared: PreparedModelGeneration,
        receipt: ModelReservationReceipt | None,
        context: InvocationContext,
        *,
        checkpoint_sink: ModelGenerationCheckpointSink | None = None,
        timeout_ms: int | None = None,
        deadline_at: datetime | None = None,
    ) -> GenerateResult:
        """只消费已预留 slot；每次 outbound 前后均通过 checkpoint fencing。"""

        if not isinstance(prepared, PreparedModelGeneration):
            raise TypeError("prepared must be PreparedModelGeneration")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")
        sink = checkpoint_sink or self._generation_checkpoint_sink
        mismatch = self._receipt_mismatch(prepared, receipt, context)
        if mismatch is not None or sink is None:
            error = ProviderError(
                "model generation receipt or checkpoint sink is unavailable",
                code=ErrorCode.MODEL_RECEIPT_MISMATCH,
                details={"reason": mismatch or "checkpoint_sink_missing"},
            )
            return GenerateResult.failure(error.to_detail())

        structured = self._prepare_structured_request(prepared.request)
        if structured is None:
            error = ProviderError(
                "prepared generation lost its strict schema",
                code=ErrorCode.MODEL_RESERVATION_INVALID,
            )
            return GenerateResult.failure(error.to_detail())
        accounting = ModelAccountingAccumulator()
        last_result: GenerateResult | None = None
        allow_next_provider = True
        previous_provider_id: str | None = None

        for slot in prepared.reservation.slots:
            if previous_provider_id == slot.provider_id:
                if last_result is not None and (
                    last_result.error is None or not last_result.error.retryable
                ):
                    continue
            elif previous_provider_id is not None and not allow_next_provider:
                break
            registration = self._registry.get_provider(slot.provider_id)
            frozen_registration = next(
                (
                    item
                    for item in prepared.registrations
                    if item.provider_id == slot.provider_id
                ),
                None,
            )
            if (
                registration is None
                or frozen_registration is None
                or registration.registration_version != slot.provider_registration_version
                or registration.model_features_hash != slot.provider_features_hash
            ):
                error = ProviderError(
                    "reserved provider registration changed",
                    code=ErrorCode.MODEL_GENERATION_ORPHANED,
                    details={"provider_id": slot.provider_id, "slot_id": slot.slot_id},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))

            try:
                ticket = await sink.start_model_generation_slot(
                    receipt,
                    prepared.reservation,
                    slot,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = ProviderError(
                    "model slot start fencing failed",
                    code=ErrorCode.MODEL_RECEIPT_MISMATCH,
                    details={"slot_id": slot.slot_id, "cause_type": type(exc).__name__},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))
            if not self._ticket_matches(receipt, prepared.reservation, slot, ticket):
                error = ProviderError(
                    "model slot execution ticket is stale",
                    code=ErrorCode.MODEL_RECEIPT_MISMATCH,
                    details={"slot_id": slot.slot_id},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))

            provider = registration.provider
            slot_prepared = prepared.prepared_by_slot[slot.slot_id]
            if not isinstance(provider, ModelProvider):
                error = ProviderError(
                    "reserved provider no longer implements ModelProvider",
                    code=ErrorCode.MODEL_GENERATION_ORPHANED,
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))
            result: GenerateResult
            reservation_violation = False
            try:
                result = await self._call_model(
                    provider,
                    prepared.request,
                    context,
                    provider_id=slot.provider_id,
                    structured=structured,
                    prepared=slot_prepared,
                    timeout_ms=timeout_ms,
                    deadline_at=self._effective_deadline(context, deadline_at),
                )
                if not isinstance(result, GenerateResult):
                    raise TypeError("provider returned an invalid generation result")
                result = result.model_copy(update={"provider_id": slot.provider_id})
                attempt_usage = accounting.record_result(
                    slot.provider_id,
                    result,
                    registration.model_features,
                )
                reservation_violation = not self._attempt_within_slot(
                    attempt_usage,
                    slot,
                    require_cost=prepared.attempt_policy.require_cost_bounds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt_usage = accounting.record_unavailable(slot.provider_id)
                error = ProviderError(
                    "reserved model provider execution failed",
                    code=ErrorCode.MODEL_ACCOUNTING_INCOMPLETE,
                    details={"slot_id": slot.slot_id, "cause_type": type(exc).__name__},
                )
                result = GenerateResult.failure(error.to_detail(), provider_id=slot.provider_id)

            try:
                self._validate_model_result(prepared.request, result, structured)
            except HarnessError as exc:
                result = GenerateResult.failure(exc.to_detail(), provider_id=slot.provider_id)
            outcome = "completed" if result.status is GenerateStatus.SUCCESS else "failed"
            if not attempt_usage.complete or reservation_violation:
                outcome = "orphaned"
            try:
                await sink.complete_model_generation_slot(
                    receipt,
                    ticket,
                    attempt_usage,
                    outcome,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = ProviderError(
                    "model slot terminal checkpoint failed after outbound",
                    code=ErrorCode.MODEL_GENERATION_ORPHANED,
                    details={"slot_id": slot.slot_id, "cause_type": type(exc).__name__},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))
            if not attempt_usage.complete:
                error = ProviderError(
                    "model attempt accounting is incomplete",
                    code=ErrorCode.MODEL_ACCOUNTING_INCOMPLETE,
                    details={"slot_id": slot.slot_id},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))
            if reservation_violation:
                error = ProviderError(
                    "model attempt exceeded or violated its reserved resource bounds",
                    code=ErrorCode.MODEL_GENERATION_ORPHANED,
                    details={"slot_id": slot.slot_id},
                )
                return accounting.attach(GenerateResult.failure(error.to_detail()))

            last_result = result
            previous_provider_id = slot.provider_id
            if result.status is GenerateStatus.SUCCESS:
                return accounting.attach(result)
            allow_next_provider = result.error is not None and result.error.fallbackable

        if last_result is None:
            error = ProviderError(
                "no reserved generation slot was executable",
                code=ErrorCode.MODEL_GENERATION_ORPHANED,
            )
            last_result = GenerateResult.failure(error.to_detail())
        return accounting.attach(last_result)

    def _resolve_model(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        deadline_at: datetime | None,
        trace: _TraceAnchor,
        trace_enabled: bool,
        structured: _StructuredRequestState | None = None,
    ) -> tuple[
        tuple[ProviderRegistration, ...],
        SelectionContext,
        SelectedProvider,
        Mapping[str, PreparedStructuredOutput | None],
    ]:
        registry_span = (
            self._tracer.start_span(
                "registry.resolve_model",
                SpanType.REGISTRY_RESOLVE,
                parent=trace.parent,
                attributes={
                    "capability_id": request.model,
                    "schema_hash": structured.schema_hash if structured is not None else None,
                },
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

        prepared_by_provider: dict[str, PreparedStructuredOutput | None] = {
            candidate.provider_id: None for candidate in candidates
        }
        if structured is not None and structured.enforce_provider_eligibility:
            candidates, prepared_by_provider = self._eligible_structured_candidates(
                candidates,
                structured,
            )
            if not candidates:
                error = ProviderError(
                    "no provider can preserve the requested structured output semantics",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
                    details={
                        "model": request.model,
                        "schema_hash": structured.schema_hash,
                        "strictness": structured.spec.strictness.value,
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
                "schema_hash": structured.schema_hash if structured is not None else None,
            },
        )
        return (
            candidates,
            selection_context,
            selected,
            MappingProxyType(prepared_by_provider),
        )

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
        structured: _StructuredRequestState | None,
        prepared: PreparedStructuredOutput | None,
        accounting: ModelAccountingAccumulator,
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
                    "schema_hash": structured.schema_hash if structured is not None else None,
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
        result_recorded = False
        try:
            result = await self._call_model(
                provider,
                request,
                execution_context,
                provider_id=selected.registration.provider_id,
                structured=structured,
                prepared=prepared,
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
            accounting.record_result(
                selected.registration.provider_id,
                result,
                selected.registration.model_features,
            )
            result_recorded = True
            self._validate_model_result(request, result, structured)
            envelope = self._to_envelope(result)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(model_span)
            raise
        except HarnessError as exc:
            if not result_recorded:
                accounting.record_unavailable(selected.registration.provider_id)
            self._lifecycle.finish_error(model_span, _safe_model_trace_error(exc))
            raise
        except Exception as exc:
            if not result_recorded:
                accounting.record_unavailable(selected.registration.provider_id)
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
            self._lifecycle.finish_error(model_span, _safe_model_trace_error(wrapped))
            raise wrapped from exc

        self._lifecycle.finish_from_result(
            model_span,
            envelope,
            error=(_safe_model_trace_error(envelope.error) if envelope.error is not None else None),
        )
        return envelope

    async def _call_model(
        self,
        provider: ModelProvider,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        provider_id: str,
        structured: _StructuredRequestState | None,
        prepared: PreparedStructuredOutput | None,
        timeout_ms: int | None,
        deadline_at: datetime | None,
    ) -> GenerateResult:
        async def invoke() -> GenerateResult:
            if prepared is None:
                if (
                    structured is not None
                    and structured.spec is not None
                    and structured.spec.strictness is StructuredOutputStrictness.REQUIRED
                ):
                    raise ProviderError(
                        "required structured output is missing provider preparation",
                        code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
                    )
                return await provider.generate(request, context)
            if prepared.provider_id != provider_id:
                raise ProviderError(
                    "prepared structured output belongs to another provider",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
                )
            if structured is None or prepared.schema_hash != structured.schema_hash:
                raise ProviderError(
                    "prepared structured output schema does not match request",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
                )
            if not prepared.semantics_preserved:
                raise ProviderError(
                    "prepared structured output does not preserve semantics",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
                )
            return await provider.generate_prepared(request, prepared, context)

        limits: list[float] = []
        if timeout_ms is not None:
            limits.append(timeout_ms / 1000)
        if deadline_at is not None:
            limits.append((deadline_at - datetime.now(UTC)).total_seconds())
        if not limits:
            return await invoke()
        remaining = min(limits)
        if remaining <= 0:
            raise HarnessTimeoutError(
                "model generation deadline exceeded",
                details={"model": request.model},
                fallbackable=True,
            )
        try:
            async with asyncio.timeout(remaining):
                return await invoke()
        except TimeoutError as exc:
            raise HarnessTimeoutError(
                "model provider timed out",
                details={"model": request.model, "timeout_ms": timeout_ms},
                fallbackable=True,
            ) from exc

    @staticmethod
    def _validate_model_result(
        request: GenerateRequest,
        result: GenerateResult,
        structured: _StructuredRequestState | None,
    ) -> None:
        if result.status is GenerateStatus.FAILED:
            return
        if result.finish_reason is ModelFinishReason.LENGTH:
            raise ProviderError(
                "model structured output was truncated",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_TRUNCATED,
                retryable=True,
                fallbackable=True,
            )
        if result.finish_reason is ModelFinishReason.REFUSAL:
            raise ProviderError(
                "model refused the structured generation request",
                code=ErrorCode.MODEL_REFUSED,
                retryable=False,
                fallbackable=True,
            )
        if result.finish_reason is ModelFinishReason.CONTENT_FILTER:
            raise ProviderError(
                "model structured output was filtered",
                code=ErrorCode.MODEL_CONTENT_FILTERED,
                retryable=False,
                fallbackable=True,
            )
        if result.output is None or result.output.type is not request.response_format:
            raise ProviderError(
                "model response format does not match request",
                code="HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID",
                retryable=True,
                fallbackable=True,
            )
        if structured is None:
            return
        if not structured.full_validation:
            ModelGateway._validate_legacy_structured_output(request, result)
            return
        if structured.validator is None:
            raise ProviderError(
                "structured output validator is unavailable",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_SCHEMA_INVALID,
            )
        data = result.output.model_dump(mode="json")["data"]
        try:
            validate_structured_value(structured.validator, data)
        except SchemaValidationFailure as exc:
            raise ProviderError(
                "model output failed local JSON Schema validation",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                details={
                    "schema_hash": structured.schema_hash,
                    "validator": exc.validator,
                    "path": list(exc.path),
                },
                retryable=True,
                fallbackable=True,
            ) from exc

    @staticmethod
    def _prepare_structured_request(
        request: GenerateRequest,
    ) -> _StructuredRequestState | None:
        spec = request.structured_output
        if spec is None:
            if request.response_schema is None:
                return None
            return _StructuredRequestState(
                spec=None,
                schema_hash=stable_request_fingerprint(
                    request.model_dump(mode="json")["response_schema"]
                ),
                validator=None,
                enforce_provider_eligibility=False,
                full_validation=False,
            )
        schema_hash = structured_schema_hash(spec)
        try:
            validator = validate_schema_definition(spec)
        except SchemaValidationFailure as exc:
            raise ProviderError(
                "structured output schema is invalid",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_SCHEMA_INVALID,
                details={
                    "schema_hash": schema_hash,
                    "validator": exc.validator,
                    "path": list(exc.path),
                },
            ) from exc
        return _StructuredRequestState(
            spec=spec,
            schema_hash=schema_hash,
            validator=validator,
            enforce_provider_eligibility=True,
            full_validation=True,
        )

    @staticmethod
    def _validate_legacy_structured_output(
        request: GenerateRequest,
        result: GenerateResult,
    ) -> None:
        schema = request.response_schema
        if schema is None or result.output is None:
            return
        data = result.output.data
        schema_type = schema.get("type")
        if schema_type == "object" and not isinstance(data, Mapping):
            raise ProviderError(
                "structured output must be an object",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                retryable=True,
                fallbackable=True,
            )
        if schema_type == "array" and not isinstance(data, tuple | list):
            raise ProviderError(
                "structured output must be an array",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                retryable=True,
                fallbackable=True,
            )
        required = schema.get("required")
        if isinstance(required, tuple | list) and isinstance(data, Mapping):
            missing = [item for item in required if isinstance(item, str) and item not in data]
            if missing:
                raise ProviderError(
                    "structured output is missing required properties",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                    details={"missing": missing},
                    retryable=True,
                    fallbackable=True,
                )

    @staticmethod
    def _eligible_structured_candidates(
        candidates: Sequence[ProviderRegistration],
        structured: _StructuredRequestState,
    ) -> tuple[
        tuple[ProviderRegistration, ...],
        dict[str, PreparedStructuredOutput | None],
    ]:
        if structured.spec is None:
            raise TypeError("strict provider eligibility requires StructuredOutputSpec")
        eligible: list[ProviderRegistration] = []
        prepared_by_provider: dict[str, PreparedStructuredOutput | None] = {}
        required = structured.spec.strictness is StructuredOutputStrictness.REQUIRED
        for registration in candidates:
            provider = registration.provider
            if not isinstance(provider, ModelProvider):
                continue
            features = registration.model_features
            can_compile = features.json_schema and (
                features.json_schema_strict or not required
            )
            prepared: PreparedStructuredOutput | None = None
            if can_compile:
                try:
                    candidate_prepared = provider.prepare_structured_output(structured.spec)
                except Exception:
                    candidate_prepared = None
                if (
                    isinstance(candidate_prepared, PreparedStructuredOutput)
                    and candidate_prepared.provider_id == registration.provider_id
                    and candidate_prepared.schema_hash == structured.schema_hash
                    and candidate_prepared.semantics_preserved
                ):
                    prepared = candidate_prepared
            if prepared is not None:
                eligible.append(registration)
                prepared_by_provider[registration.provider_id] = prepared
                continue
            if (
                not required
                and structured.spec.on_unsupported
                is UnsupportedStructuredOutputBehavior.JSON_OBJECT
                and features.json_object
            ):
                eligible.append(registration)
                prepared_by_provider[registration.provider_id] = None
        return tuple(eligible), prepared_by_provider

    def _ordered_reservation_candidates(
        self,
        candidates: Sequence[ProviderRegistration],
        selection_context: SelectionContext,
        selected: SelectedProvider,
        *,
        max_count: int,
    ) -> tuple[ProviderRegistration, ...]:
        ordered = [selected.registration]
        side_effect = selected.registration.capability.execution_profile.side_effect
        remaining = [
            item
            for item in candidates
            if item.provider_id != selected.registration.provider_id
            and (
                side_effect in {SideEffectType.NONE, SideEffectType.READ}
                or (
                    item.descriptor.equivalence_group is not None
                    and item.descriptor.equivalence_group
                    == selected.registration.descriptor.equivalence_group
                )
            )
        ]
        while remaining and len(ordered) < max_count:
            try:
                next_selected = self._provider_execution.select(remaining, selection_context)
            except HarnessError:
                break
            ordered.append(next_selected.registration)
            remaining = [
                item
                for item in remaining
                if item.provider_id != next_selected.registration.provider_id
            ]
        return tuple(ordered)

    @staticmethod
    def _receipt_mismatch(
        prepared: PreparedModelGeneration,
        receipt: ModelReservationReceipt | None,
        context: InvocationContext,
    ) -> str | None:
        if not isinstance(receipt, ModelReservationReceipt):
            return "receipt_missing"
        reservation = prepared.reservation
        reservation_payload = reservation.model_dump(mode="json")
        reservation_hash = reservation_payload.pop("reservation_hash")
        if stable_request_fingerprint(reservation_payload) != reservation_hash:
            return "reservation_integrity"
        if receipt.generation_id != reservation.generation_id:
            return "generation_id"
        if receipt.reservation_hash != reservation.reservation_hash:
            return "reservation_hash"
        if stable_request_fingerprint(prepared.request.model_dump(mode="json")) != (
            reservation.request_fingerprint
        ):
            return "request_fingerprint"
        registry_snapshot_hash = stable_request_fingerprint(
            [
                {
                    "provider_id": registration.provider_id,
                    "registration_version": registration.registration_version,
                    "features_hash": registration.model_features_hash,
                }
                for registration in prepared.registrations
            ]
        )
        if registry_snapshot_hash != reservation.registry_snapshot_hash:
            return "registry_snapshot"
        plan_id = context.attributes.get("plan_id")
        node_id = context.attributes.get("node_id")
        if plan_id != receipt.execution_ref.plan_id:
            return "plan_id"
        if node_id != receipt.execution_ref.node_id:
            return "node_id"
        exploration_id = context.attributes.get("exploration_id")
        if exploration_id != receipt.exploration_id:
            return "exploration_id"
        return None

    @staticmethod
    def _ticket_matches(
        receipt: ModelReservationReceipt,
        reservation: ModelGenerationReservation,
        slot: ModelGenerationAttemptSlot,
        ticket: object,
    ) -> bool:
        return (
            isinstance(ticket, ModelSlotExecutionTicket)
            and ticket.generation_id == reservation.generation_id
            and ticket.slot_id == slot.slot_id
            and ticket.reservation_hash == reservation.reservation_hash
            and ticket.committed_state_version >= receipt.committed_state_version
            and ticket.scheduler_generation == receipt.scheduler_generation
            and ticket.owner_epoch == receipt.owner_epoch
        )

    @staticmethod
    def _attempt_within_slot(
        accounting: ModelProviderAttemptUsage,
        slot: ModelGenerationAttemptSlot,
        *,
        require_cost: bool,
    ) -> bool:
        usage = accounting.usage
        if usage is None:
            return False
        if (
            usage.input_tokens > slot.input_token_upper_bound
            or usage.output_tokens > slot.output_token_upper_bound
            or usage.total_tokens > slot.token_upper_bound
        ):
            return False
        actual_cost = accounting.normalized_cost
        reserved_cost = slot.normalized_cost_upper_bound
        if require_cost and (actual_cost is None or reserved_cost is None):
            return False
        if actual_cost is None:
            return True
        return (
            reserved_cost is not None
            and actual_cost.unit == reserved_cost.unit
            and actual_cost.amount <= reserved_cost.amount
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


def _safe_model_trace_error(error: object) -> TraceError:
    """MODEL Span 只保留稳定错误码，不复制 Provider/模型返回的原始消息。"""

    raw_code = getattr(error, "code", None)
    code = (
        raw_code
        if isinstance(raw_code, str)
        and 0 < len(raw_code) <= 160
        and all(character.isalnum() or character in "._:/-" for character in raw_code)
        else "UNSAFE_ERROR_CODE_REDACTED"
    )
    return TraceError(
        type="ModelError",
        message="model generation failed",
        code=code,
    )


@dataclass(frozen=True, slots=True)
class _StructuredRequestState:
    spec: StructuredOutputSpec | None
    schema_hash: str
    validator: object | None
    enforce_provider_eligibility: bool
    full_validation: bool


@dataclass(slots=True)
class _TraceAnchor:
    parent: ModelInvocationParent

    def capture(self, span: Span | None) -> None:
        if self.parent is None and span is not None:
            self.parent = TraceContext(trace_id=span.trace_id)
