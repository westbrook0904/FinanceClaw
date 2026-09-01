"""统一 Request 入口的路由、规划与执行编排。"""

from __future__ import annotations

import asyncio

from harness_context import ContextPipeline
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ContextConsumer,
    ContextUseRecord,
    ErrorCode,
    ExecutionMode,
    HarnessError,
    InvocationContext,
    JsonValue,
    PlanningError,
    PolicyError,
    Request,
    RequestError,
    ResultEnvelope,
    RouteDecision,
    RoutingError,
)
from harness_events import EventPublisher, ExecutionEventName
from harness_execution import ExecutionEngine
from harness_planning import (
    PlanMaterializer,
    Planner,
    PlannerOutputNormalizer,
    PlannerRegistry,
    PlanningAttempt,
    PlanningConstraints,
    PlanningContext,
    PlanValidationError,
)
from harness_policy import PolicyEngine, PreRoutePolicyResult
from harness_registry import CapabilityCatalog
from harness_routing import (
    RequestProjector,
    RequestSummary,
    RouteDecisionValidator,
    Router,
    RoutingContext,
)
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_trace import Span, SpanType, TraceError, Tracer

from .observability import (
    RequestEventEmitter,
    safe_observation_code,
    stable_observation_hash,
)


def normalize_request_mode(
    request: Request,
    mode: ExecutionMode | str | None,
) -> Request:
    """把 ``handle(..., mode=...)`` sugar 归一化到一个不可变 Request 副本。"""

    if not isinstance(request, Request):
        raise TypeError("request must be Request")
    if mode is None:
        return request
    if not isinstance(mode, ExecutionMode):
        try:
            mode = ExecutionMode(mode)
        except (TypeError, ValueError) as exc:
            raise RequestError(
                "handle mode must be a valid ExecutionMode",
                code=ErrorCode.REQUEST_INVALID,
                details={"mode": str(mode)},
            ) from exc

    request_mode = request.options.execution_mode
    if (
        request_mode is not ExecutionMode.AUTO
        and mode is not ExecutionMode.AUTO
        and request_mode is not mode
    ):
        raise RequestError(
            "handle mode conflicts with request execution_mode",
            code=ErrorCode.REQUEST_MODE_CONFLICT,
            details={
                "request_mode": request_mode.value,
                "handle_mode": mode.value,
            },
        )

    if request_mode is not ExecutionMode.AUTO or mode is ExecutionMode.AUTO:
        return request

    options = request.options.model_copy(update={"execution_mode": mode})
    return request.model_copy(update={"options": options})


class RequestCoordinator:
    """在一个 Request 生命周期内执行 PRE_ROUTE、路由、规划与调度。"""

    def __init__(
        self,
        router: Router,
        decision_validator: RouteDecisionValidator,
        request_projector: RequestProjector,
        policy_engine: PolicyEngine,
        context_pipeline: ContextPipeline,
        capability_catalog: CapabilityCatalog,
        invoker: CapabilityInvoker,
        execution_engine: ExecutionEngine,
        lifecycle: InvocationLifecycle,
        tracer: Tracer,
        event_publisher: EventPublisher,
        planner_registry: PlannerRegistry,
        default_planner_id: str | None,
        planner_output_normalizer: PlannerOutputNormalizer | None = None,
        plan_materializer: PlanMaterializer | None = None,
    ) -> None:
        if not isinstance(router, Router):
            raise TypeError("router must implement Router")
        if not isinstance(decision_validator, RouteDecisionValidator):
            raise TypeError("decision_validator must be RouteDecisionValidator")
        if not isinstance(request_projector, RequestProjector):
            raise TypeError("request_projector must implement RequestProjector")
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        if not isinstance(context_pipeline, ContextPipeline):
            raise TypeError("context_pipeline must be ContextPipeline")
        if not isinstance(capability_catalog, CapabilityCatalog):
            raise TypeError("capability_catalog must implement CapabilityCatalog")
        if not isinstance(invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if not isinstance(execution_engine, ExecutionEngine):
            raise TypeError("execution_engine must be ExecutionEngine")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(event_publisher, EventPublisher):
            raise TypeError("event_publisher must implement EventPublisher")
        if not isinstance(planner_registry, PlannerRegistry):
            raise TypeError("planner_registry must be PlannerRegistry")
        if default_planner_id is not None:
            planner_registry.get(default_planner_id)
        if planner_output_normalizer is not None and not isinstance(
            planner_output_normalizer,
            PlannerOutputNormalizer,
        ):
            raise TypeError("planner_output_normalizer must be PlannerOutputNormalizer")
        if plan_materializer is not None and not isinstance(plan_materializer, PlanMaterializer):
            raise TypeError("plan_materializer must be PlanMaterializer")
        if lifecycle.tracer is not tracer:
            raise ValueError("lifecycle and coordinator must use the same tracer")
        if invoker.lifecycle is not lifecycle:
            raise ValueError("invoker and coordinator must use the same lifecycle")
        if invoker.policy_engine is not policy_engine:
            raise ValueError("invoker and coordinator must use the same policy_engine")
        if context_pipeline.policy.policy_engine is not policy_engine:
            raise ValueError("context_pipeline and coordinator must use the same policy_engine")
        if invoker.tracer is not tracer:
            raise ValueError("invoker and coordinator must use the same tracer")
        if invoker.event_publisher is not event_publisher:
            raise ValueError("invoker and coordinator must use the same event_publisher")
        if execution_engine.event_publisher is not event_publisher:
            raise ValueError("execution_engine and coordinator must use the same event_publisher")

        self._router = router
        self._decision_validator = decision_validator
        self._request_projector = request_projector
        self._policy_engine = policy_engine
        self._context_pipeline = context_pipeline
        self._capability_catalog = capability_catalog
        self._invoker = invoker
        self._execution_engine = execution_engine
        self._lifecycle = lifecycle
        self._tracer = tracer
        self._events = RequestEventEmitter(event_publisher)
        self._planner_registry = planner_registry
        self._default_planner_id = default_planner_id
        self._planner_output_normalizer = planner_output_normalizer or PlannerOutputNormalizer()
        self._plan_materializer = plan_materializer or PlanMaterializer()

    @property
    def router(self) -> Router:
        return self._router

    @property
    def decision_validator(self) -> RouteDecisionValidator:
        return self._decision_validator

    @property
    def request_projector(self) -> RequestProjector:
        return self._request_projector

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    @property
    def context_pipeline(self) -> ContextPipeline:
        return self._context_pipeline

    @property
    def capability_catalog(self) -> CapabilityCatalog:
        return self._capability_catalog

    @property
    def invoker(self) -> CapabilityInvoker:
        return self._invoker

    @property
    def execution_engine(self) -> ExecutionEngine:
        return self._execution_engine

    @property
    def lifecycle(self) -> InvocationLifecycle:
        return self._lifecycle

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def event_publisher(self) -> EventPublisher:
        return self._events.publisher

    @property
    def planner_registry(self) -> PlannerRegistry:
        return self._planner_registry

    @property
    def planner_output_normalizer(self) -> PlannerOutputNormalizer:
        return self._planner_output_normalizer

    @property
    def plan_materializer(self) -> PlanMaterializer:
        return self._plan_materializer

    @property
    def default_planner_id(self) -> str | None:
        return self._default_planner_id

    async def handle(self, request: Request) -> ResultEnvelope:
        """用单一 Context、Deadline 与 REQUEST span 完成一次请求调度。"""

        if not isinstance(request, Request):
            raise TypeError("request must be Request")

        context_result = self._lifecycle.create_context(request)
        if isinstance(context_result, ResultEnvelope):
            return context_result
        context = context_result

        trace_enabled = request.options.trace
        request_span = self._lifecycle.start_request_span(context) if trace_enabled else None
        runtime_span = (
            self._tracer.start_span(
                "runtime.handle",
                SpanType.RUNTIME,
                parent=request_span,
                attributes={
                    "request_id": request.request_id,
                    "requested_mode": request.options.execution_mode.value,
                },
            )
            if trace_enabled
            else None
        )
        if runtime_span is not None:
            context = self._lifecycle.with_trace_context(context, runtime_span)

        try:
            pre_route = self._evaluate_pre_route(
                context,
                request.options.execution_mode,
                parent=runtime_span,
                trace_enabled=trace_enabled,
            )
            request_summary = self._request_projector.project(request)
            catalog_snapshot = self._capability_catalog.list()
            route_bundle = await self._context_pipeline.build(
                context,
                ContextConsumer.ROUTE,
                request_projection=_model_request_projection(request_summary),
                capability_catalog=_model_catalog(
                    catalog_snapshot,
                    pre_route.constraints.allowed_capability_ids,
                ),
            )
            routing_context = RoutingContext(
                invocation=context,
                request_summary=request_summary,
                requested_mode=pre_route.effective_mode,
                catalog_snapshot=catalog_snapshot,
                constraints=pre_route.constraints,
                projection=route_bundle.projection,
                context_use=route_bundle.use_record,
            )
            decision = await self._route(
                routing_context,
                requested_mode=request.options.execution_mode,
                effective_mode=pre_route.effective_mode,
                parent=runtime_span,
                trace_enabled=trace_enabled,
            )
            result, planner_id = await self._dispatch(
                request,
                context,
                decision,
                routing_context=routing_context,
                parent=runtime_span,
                trace_enabled=trace_enabled,
            )
            result = self._with_route_metadata(result, decision, planner_id=planner_id)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise
        except PolicyError as exc:
            result = ResultEnvelope.denied(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            trace_error = self._safe_trace_error(exc, message="request policy denied")
            self._lifecycle.finish_from_result(runtime_span, result, error=trace_error)
            self._lifecycle.finish_from_result(request_span, result, error=trace_error)
            return result
        except HarnessError as exc:
            result = ResultEnvelope.failure(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            trace_error = self._safe_trace_error(exc, message="request handling failed")
            self._lifecycle.finish_from_result(runtime_span, result, error=trace_error)
            self._lifecycle.finish_from_result(request_span, result, error=trace_error)
            return result
        except Exception as exc:
            wrapped = RoutingError(
                "request routing failed",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self._router.router_id,
                    "cause_type": type(exc).__name__,
                },
            )
            result = ResultEnvelope.failure(
                wrapped.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            trace_error = self._safe_trace_error(wrapped, message="request handling failed")
            self._lifecycle.finish_from_result(runtime_span, result, error=trace_error)
            self._lifecycle.finish_from_result(request_span, result, error=trace_error)
            return result

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result

    def _evaluate_pre_route(
        self,
        context: InvocationContext,
        requested_mode: ExecutionMode,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> PreRoutePolicyResult:
        span = (
            self._tracer.start_span(
                "policy.pre_route",
                SpanType.POLICY,
                parent=parent,
                attributes={"requested_mode": requested_mode.value},
            )
            if trace_enabled
            else None
        )
        policy_context = (
            self._lifecycle.with_trace_context(context, span) if span is not None else context
        )
        try:
            result = self._policy_engine.evaluate_pre_route(policy_context, requested_mode)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(
                span,
                self._safe_trace_error(exc, message="pre-route policy failed"),
            )
            raise
        except Exception as exc:
            wrapped = PolicyError(
                "pre-route policy evaluation failed",
                code="HARNESS.POLICY.EVALUATION_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(
                span,
                self._safe_trace_error(wrapped, message="pre-route policy failed"),
            )
            raise wrapped from exc

        self._lifecycle.finish_ok(
            span,
            attributes={
                "effect": result.decision.effect.value,
                "effective_mode": result.effective_mode.value,
                "policy": result.decision.policy,
            },
        )
        return result

    async def _route(
        self,
        routing_context: RoutingContext,
        *,
        requested_mode: ExecutionMode,
        effective_mode: ExecutionMode,
        parent: Span | None,
        trace_enabled: bool,
    ) -> RouteDecision:
        request_id = routing_context.invocation.request.request_id
        catalog_hash = stable_observation_hash(
            [descriptor.model_dump(mode="json") for descriptor in routing_context.catalog_snapshot]
        )
        summary_hash = stable_observation_hash(
            routing_context.request_summary.model_dump(mode="json")
        )
        context_attributes = _context_trace_attributes(routing_context.context_use)
        route_span = (
            self._tracer.start_span(
                f"route.{self._router.router_id}",
                SpanType.ROUTE,
                parent=parent,
                attributes={
                    "router_id": self._router.router_id,
                    "requested_mode": requested_mode.value,
                    "effective_mode": effective_mode.value,
                    "catalog_snapshot_hash": catalog_hash,
                    "request_summary_hash": summary_hash,
                    **context_attributes,
                },
            )
            if trace_enabled
            else None
        )
        observed_context = routing_context
        if route_span is not None:
            observed_context = routing_context.model_copy(
                update={
                    "invocation": self._lifecycle.with_trace_context(
                        routing_context.invocation,
                        route_span,
                    )
                }
            )

        try:
            decision = await self._router.route(observed_context)
            decision = self._decision_validator.validate(decision, observed_context)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(route_span)
            raise
        except HarnessError as exc:
            safe_error_code = safe_observation_code(
                exc.code,
                fallback="UNSAFE_ERROR_CODE_REDACTED",
            )
            await self._emit_route_failed(
                request_id,
                safe_error_code,
                route_span=route_span,
            )
            self._lifecycle.finish_error(
                route_span,
                self._safe_trace_error(exc, message="route failed"),
                attributes={"error_code": safe_error_code},
            )
            raise
        except Exception as exc:
            wrapped = RoutingError(
                "request routing failed",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self._router.router_id,
                    "cause_type": type(exc).__name__,
                },
            )
            safe_error_code = safe_observation_code(
                wrapped.code,
                fallback="UNSAFE_ERROR_CODE_REDACTED",
            )
            await self._emit_route_failed(
                request_id,
                safe_error_code,
                route_span=route_span,
            )
            self._lifecycle.finish_error(
                route_span,
                self._safe_trace_error(wrapped, message="route failed"),
                attributes={"error_code": safe_error_code},
            )
            raise wrapped from exc

        mode_source = "policy" if requested_mode is not effective_mode else decision.source.value
        safe_reason_code = safe_observation_code(
            decision.reason_code,
            fallback="UNSAFE_REASON_CODE_REDACTED",
        )
        route_attributes: dict[str, JsonValue] = {
            "router_id": self._router.router_id,
            "route_type": decision.route_type.value,
            "decision_source": decision.source.value,
            "reason_code": safe_reason_code,
        }
        if decision.confidence is not None:
            route_attributes["confidence"] = decision.confidence
        await self._events.emit(
            ExecutionEventName.ROUTE_DECIDED,
            request_id=request_id,
            trace_id=route_span.trace_id if route_span is not None else None,
            attributes={
                "router_id": self._router.router_id,
                "mode": decision.mode.value,
                "route_type": decision.route_type.value,
                "reason_code": safe_reason_code,
                **({"confidence": decision.confidence} if decision.confidence is not None else {}),
            },
        )
        await self._events.emit(
            ExecutionEventName.MODE_SELECTED,
            request_id=request_id,
            trace_id=route_span.trace_id if route_span is not None else None,
            attributes={
                "requested_mode": requested_mode.value,
                "selected_mode": decision.mode.value,
                "source": mode_source,
            },
        )
        self._lifecycle.finish_ok(route_span, attributes=route_attributes)
        return decision

    async def _emit_route_failed(
        self,
        request_id: str,
        error_code: str,
        *,
        route_span: Span | None,
    ) -> None:
        await self._events.emit(
            ExecutionEventName.ROUTE_FAILED,
            request_id=request_id,
            trace_id=route_span.trace_id if route_span is not None else None,
            attributes={
                "router_id": self._router.router_id,
                "error_code": error_code,
            },
        )

    async def _dispatch(
        self,
        request: Request,
        context: InvocationContext,
        decision: RouteDecision,
        *,
        routing_context: RoutingContext,
        parent: Span | None,
        trace_enabled: bool,
    ) -> tuple[ResultEnvelope, str | None]:
        if decision.mode is ExecutionMode.FAST and decision.capability_id is not None:
            caller_plugin_id = request.target.plugin if request.target is not None else None
            result = await self._invoker.invoke(
                decision.capability_id,
                request.input,
                context,
                plugin_id=caller_plugin_id,
                deadline_at=context.deadline_at,
                parent=parent,
                trace_enabled=trace_enabled,
            )
            return result, None

        planner = self._select_planner(routing_context)
        result = await self._dispatch_plan(
            request,
            context,
            planner,
            routing_context=routing_context,
            parent=parent,
            trace_enabled=trace_enabled,
        )
        return result, planner.planner_id

    async def _dispatch_plan(
        self,
        request: Request,
        context: InvocationContext,
        planner: Planner,
        *,
        routing_context: RoutingContext,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        prompt_version = self._planner_prompt_version(planner)
        route_constraints = routing_context.constraints
        plan_bundle = await self._context_pipeline.build(
            context,
            ContextConsumer.PLAN,
            request_projection=_model_request_projection(routing_context.request_summary),
            capability_catalog=_model_catalog(
                routing_context.catalog_snapshot,
                route_constraints.allowed_capability_ids,
            ),
        )
        catalog_hash = stable_observation_hash(
            [descriptor.model_dump(mode="json") for descriptor in routing_context.catalog_snapshot]
        )
        planner_span = (
            self._tracer.start_span(
                f"planner.{planner.planner_id}",
                SpanType.PLANNER,
                parent=parent,
                attributes={
                    "planner_id": planner.planner_id,
                    "prompt_version": prompt_version,
                    "catalog_snapshot_hash": catalog_hash,
                    **_context_trace_attributes(plan_bundle.use_record),
                },
            )
            if trace_enabled
            else None
        )
        planner_invocation = (
            self._lifecycle.with_trace_context(context, planner_span)
            if planner_span is not None
            else context
        )
        planning_context = PlanningContext(
            invocation=planner_invocation,
            goal=routing_context.request_summary,
            catalog_snapshot=routing_context.catalog_snapshot,
            constraints=PlanningConstraints(
                max_plan_attempts=route_constraints.max_plan_attempts or 3,
                max_plan_nodes=route_constraints.max_plan_nodes or 32,
                allowed_capability_ids=route_constraints.allowed_capability_ids,
                deadline_at=context.deadline_at,
            ),
            projection=plan_bundle.projection,
            context_use=plan_bundle.use_record,
        )
        attempts: list[PlanningAttempt] = []

        async def observe_attempt(attempt: PlanningAttempt) -> None:
            attempts.append(attempt)
            if not attempt.repair_scheduled:
                return
            validation_codes = [
                safe_observation_code(
                    value,
                    fallback="UNSAFE_VALIDATION_CODE_REDACTED",
                )
                for value in attempt.validation_codes[:16]
            ]
            attributes: dict[str, JsonValue] = {
                "planner_id": planner.planner_id,
                "attempt": attempt.attempt + 1,
                "validation_codes": validation_codes,
            }
            if planner_span is not None:
                try:
                    self._tracer.add_event(
                        planner_span,
                        ExecutionEventName.PLANNER_REPAIRING.value,
                        attributes=attributes,
                    )
                except Exception:
                    pass
            await self._events.emit(
                ExecutionEventName.PLANNER_REPAIRING,
                request_id=request.request_id,
                trace_id=planner_span.trace_id if planner_span is not None else None,
                attributes=attributes,
            )

        try:
            await self._events.emit(
                ExecutionEventName.PLANNER_STARTED,
                request_id=request.request_id,
                trace_id=planner_span.trace_id if planner_span is not None else None,
                attributes={
                    "planner_id": planner.planner_id,
                    "prompt_version": prompt_version,
                    "max_attempts": planning_context.constraints.max_plan_attempts,
                },
            )
            artifact = await planner.plan_artifact_with_observer(
                planning_context,
                attempt_observer=observe_attempt,
            )
            template = self._planner_output_normalizer.normalize(
                artifact,
                planner_id=planner.planner_id,
            )
            plan = self._plan_materializer.materialize(
                template,
                planner_invocation,
                planner_id=planner.planner_id,
                trusted_metadata={"prompt_version": prompt_version},
            )
            try:
                self._execution_engine.validator.validate(plan)
            except PlanValidationError as exc:
                raise PlanningError(
                    "planner returned an invalid plan artifact",
                    code=ErrorCode.PLANNER_INVALID_OUTPUT,
                    details={
                        "planner_id": planner.planner_id,
                        "issue_count": len(exc.issues),
                        "validation_codes": sorted({issue.code.value for issue in exc.issues}),
                    },
                ) from exc
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(planner_span)
            raise
        except HarnessError as exc:
            attempt_count = self._planner_attempt_count(attempts, error=exc)
            validation_codes = self._planner_validation_codes(attempts, error=exc)
            safe_error_code = safe_observation_code(
                exc.code,
                fallback="UNSAFE_ERROR_CODE_REDACTED",
            )
            await self._emit_planner_failed(
                request,
                planner,
                attempt_count=attempt_count,
                error_code=safe_error_code,
                validation_codes=validation_codes,
                planner_span=planner_span,
            )
            self._lifecycle.finish_error(
                planner_span,
                self._safe_trace_error(exc, message="planner failed"),
                attributes={
                    "attempt_count": attempt_count,
                    "validation_result": "failed",
                    "error_code": safe_error_code,
                    "validation_codes": list(validation_codes),
                },
            )
            raise
        except Exception as exc:
            wrapped = PlanningError(
                "planner execution failed",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": planner.planner_id,
                    "cause_type": type(exc).__name__,
                },
            )
            attempt_count = self._planner_attempt_count(attempts, error=wrapped)
            validation_codes = self._planner_validation_codes(attempts, error=wrapped)
            safe_error_code = safe_observation_code(
                wrapped.code,
                fallback="UNSAFE_ERROR_CODE_REDACTED",
            )
            await self._emit_planner_failed(
                request,
                planner,
                attempt_count=attempt_count,
                error_code=safe_error_code,
                validation_codes=validation_codes,
                planner_span=planner_span,
            )
            self._lifecycle.finish_error(
                planner_span,
                self._safe_trace_error(wrapped, message="planner failed"),
                attributes={
                    "attempt_count": attempt_count,
                    "validation_result": "failed",
                    "error_code": safe_error_code,
                    "validation_codes": list(validation_codes),
                },
            )
            raise wrapped from exc

        attempt_count = self._planner_attempt_count(attempts)
        await self._events.emit(
            ExecutionEventName.PLANNER_COMPLETED,
            request_id=request.request_id,
            plan_id=plan.plan_id,
            trace_id=planner_span.trace_id if planner_span is not None else None,
            attributes={
                "planner_id": planner.planner_id,
                "attempt_count": attempt_count,
                "plan_id": plan.plan_id,
                "node_count": len(plan.nodes),
            },
        )
        self._lifecycle.finish_ok(
            planner_span,
            attributes={
                "attempt_count": attempt_count,
                "plan_id": plan.plan_id,
                "plan_revision": plan.revision,
                "node_count": len(plan.nodes),
                "validation_result": "valid",
            },
        )
        return await self._execution_engine.execute_with_context(
            request,
            plan,
            context,
            parent=parent,
        )

    async def _emit_planner_failed(
        self,
        request: Request,
        planner: Planner,
        *,
        attempt_count: int,
        error_code: str,
        validation_codes: tuple[str, ...],
        planner_span: Span | None,
    ) -> None:
        await self._events.emit(
            ExecutionEventName.PLANNER_FAILED,
            request_id=request.request_id,
            trace_id=planner_span.trace_id if planner_span is not None else None,
            attributes={
                "planner_id": planner.planner_id,
                "attempt_count": attempt_count,
                "error_code": error_code,
                "validation_codes": list(validation_codes),
            },
        )

    @staticmethod
    def _planner_prompt_version(planner: Planner) -> str:
        try:
            value = getattr(planner, "prompt_version", None)
        except Exception:
            return "not_applicable"
        return safe_observation_code(value, fallback="not_applicable")

    @staticmethod
    def _safe_trace_error(error: HarnessError, *, message: str) -> TraceError:
        return TraceError(
            type="HarnessError",
            message=message,
            code=safe_observation_code(
                error.code,
                fallback="UNSAFE_ERROR_CODE_REDACTED",
            ),
        )

    @staticmethod
    def _planner_attempt_count(
        attempts: list[PlanningAttempt],
        *,
        error: HarnessError | None = None,
    ) -> int:
        if error is not None:
            detail_count = error.details.get("attempt_count")
            if (
                isinstance(detail_count, int)
                and not isinstance(detail_count, bool)
                and detail_count >= 0
            ):
                return detail_count
        return max((attempt.attempt for attempt in attempts), default=1)

    @staticmethod
    def _planner_validation_codes(
        attempts: list[PlanningAttempt],
        *,
        error: HarnessError,
    ) -> tuple[str, ...]:
        values: object = error.details.get("validation_codes")
        if not isinstance(values, list | tuple):
            values = attempts[-1].validation_codes if attempts else ()
        return tuple(
            safe_observation_code(value, fallback="UNSAFE_VALIDATION_CODE_REDACTED")
            for value in values[:16]
        )

    def _select_planner(self, routing_context: RoutingContext) -> Planner:
        """由受信任配置与 PRE_ROUTE Policy 选择 Planner，而不是接受模型选择。"""

        planner_id = self._default_planner_id
        if planner_id is None:
            raise PlanningError(
                "PLAN mode requires a configured default planner",
                code=ErrorCode.PLANNER_NOT_CONFIGURED,
                details={"router_id": self._router.router_id},
            )
        planner = self._planner_registry.get(planner_id)
        allowed = routing_context.constraints.allowed_planner_ids
        if allowed is not None and planner.planner_id not in allowed:
            raise RoutingError(
                "configured planner is not allowed by policy",
                code=ErrorCode.ROUTE_PLANNER_NOT_ALLOWED,
                details={"planner_id": planner.planner_id},
            )
        return planner

    def _with_route_metadata(
        self,
        result: ResultEnvelope,
        decision: RouteDecision,
        *,
        planner_id: str | None,
    ) -> ResultEnvelope:
        payload = result.model_dump(mode="json")
        metadata = dict(payload["metadata"])
        metadata.update(
            {
                "execution_mode": decision.mode.value,
                "route_type": decision.route_type.value,
                "route_reason_code": decision.reason_code,
                "router_id": self._router.router_id,
                "capability_id": decision.capability_id,
            }
        )
        if planner_id is not None:
            metadata["planner_id"] = planner_id
        payload["metadata"] = metadata
        return ResultEnvelope.model_validate(payload)


def _model_request_projection(summary: RequestSummary) -> dict[str, JsonValue]:
    payload = summary.model_dump(mode="json", exclude={"request_id"})
    return {key: value for key, value in payload.items()}


def _model_catalog(
    catalog: tuple[CapabilityDescriptor, ...],
    allowed_capability_ids: frozenset[str] | None,
) -> tuple[CapabilityDescriptor, ...]:
    return tuple(
        descriptor
        for descriptor in catalog
        if descriptor.type in {CapabilityType.AGENT, CapabilityType.TOOL}
        and (allowed_capability_ids is None or descriptor.id in allowed_capability_ids)
    )


def _context_trace_attributes(
    use_record: ContextUseRecord | None,
) -> dict[str, JsonValue]:
    if use_record is None:
        return {}
    return {
        "context_snapshot_hash": use_record.snapshot_hash,
        "context_projection_hash": use_record.projection_hash,
        "context_included_count": len(use_record.included_item_ids),
        "context_omitted_count": len(use_record.omitted),
    }
