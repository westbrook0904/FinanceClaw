"""统一 Request 入口的路由与 FAST 执行编排。"""

from __future__ import annotations

import asyncio

from harness_contracts import (
    ErrorCode,
    ExecutionMode,
    HarnessError,
    InvocationContext,
    PlanningError,
    PolicyError,
    Request,
    RequestError,
    ResultEnvelope,
    RouteDecision,
    RoutingError,
)
from harness_planning import PlannerRegistry
from harness_policy import PolicyEngine, PreRoutePolicyResult
from harness_registry import CapabilityCatalog
from harness_routing import (
    RequestProjector,
    RouteDecisionValidator,
    Router,
    RoutingContext,
)
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_trace import Span, SpanType, Tracer


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
    """在一个 Request 生命周期内执行 PRE_ROUTE、路由校验与 FAST 调用。"""

    def __init__(
        self,
        router: Router,
        decision_validator: RouteDecisionValidator,
        request_projector: RequestProjector,
        policy_engine: PolicyEngine,
        capability_catalog: CapabilityCatalog,
        invoker: CapabilityInvoker,
        lifecycle: InvocationLifecycle,
        tracer: Tracer,
        planner_registry: PlannerRegistry,
        default_planner_id: str | None,
    ) -> None:
        if not isinstance(router, Router):
            raise TypeError("router must implement Router")
        if not isinstance(decision_validator, RouteDecisionValidator):
            raise TypeError("decision_validator must be RouteDecisionValidator")
        if not isinstance(request_projector, RequestProjector):
            raise TypeError("request_projector must implement RequestProjector")
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        if not isinstance(capability_catalog, CapabilityCatalog):
            raise TypeError("capability_catalog must implement CapabilityCatalog")
        if not isinstance(invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(planner_registry, PlannerRegistry):
            raise TypeError("planner_registry must be PlannerRegistry")
        if default_planner_id is not None:
            planner_registry.get(default_planner_id)
        if lifecycle.tracer is not tracer:
            raise ValueError("lifecycle and coordinator must use the same tracer")
        if invoker.lifecycle is not lifecycle:
            raise ValueError("invoker and coordinator must use the same lifecycle")
        if invoker.policy_engine is not policy_engine:
            raise ValueError("invoker and coordinator must use the same policy_engine")
        if invoker.tracer is not tracer:
            raise ValueError("invoker and coordinator must use the same tracer")

        self._router = router
        self._decision_validator = decision_validator
        self._request_projector = request_projector
        self._policy_engine = policy_engine
        self._capability_catalog = capability_catalog
        self._invoker = invoker
        self._lifecycle = lifecycle
        self._tracer = tracer
        self._planner_registry = planner_registry
        self._default_planner_id = default_planner_id

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
    def capability_catalog(self) -> CapabilityCatalog:
        return self._capability_catalog

    @property
    def invoker(self) -> CapabilityInvoker:
        return self._invoker

    @property
    def lifecycle(self) -> InvocationLifecycle:
        return self._lifecycle

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def planner_registry(self) -> PlannerRegistry:
        return self._planner_registry

    @property
    def default_planner_id(self) -> str | None:
        return self._default_planner_id

    async def handle(self, request: Request) -> ResultEnvelope:
        """用单一 Context、Deadline 与 REQUEST span 完成一次 FAST 调度。"""

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
            routing_context = RoutingContext(
                invocation=context,
                request_summary=self._request_projector.project(request),
                requested_mode=pre_route.effective_mode,
                catalog_snapshot=self._capability_catalog.list(),
                constraints=pre_route.constraints,
            )
            decision = await self._router.route(routing_context)
            decision = self._decision_validator.validate(decision, routing_context)
            result = await self._dispatch_fast(
                request,
                context,
                decision,
                routing_context=routing_context,
                parent=runtime_span,
                trace_enabled=trace_enabled,
            )
            result = self._with_route_metadata(result, decision)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise
        except PolicyError as exc:
            result = ResultEnvelope.denied(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            self._lifecycle.finish_from_result(runtime_span, result, error=exc)
            self._lifecycle.finish_from_result(request_span, result, error=exc)
            return result
        except HarnessError as exc:
            result = ResultEnvelope.failure(
                exc.to_detail(),
                trace_id=request_span.trace_id if request_span is not None else None,
            )
            self._lifecycle.finish_from_result(runtime_span, result, error=exc)
            self._lifecycle.finish_from_result(request_span, result, error=exc)
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
            self._lifecycle.finish_from_result(runtime_span, result, error=wrapped)
            self._lifecycle.finish_from_result(request_span, result, error=wrapped)
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
            self._lifecycle.finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = PolicyError(
                "pre-route policy evaluation failed",
                code="HARNESS.POLICY.EVALUATION_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(span, wrapped)
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

    async def _dispatch_fast(
        self,
        request: Request,
        context: InvocationContext,
        decision: RouteDecision,
        *,
        routing_context: RoutingContext,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        if decision.mode is not ExecutionMode.FAST or decision.capability_id is None:
            planner_id = self._select_planner_id(routing_context)
            raise RoutingError(
                "PLAN request dispatch is not available before the shared lifecycle path",
                code=ErrorCode.ROUTE_MODE_NOT_AVAILABLE,
                details={
                    "execution_mode": decision.mode.value,
                    "planner_id": planner_id,
                },
            )

        caller_plugin_id = request.target.plugin if request.target is not None else None
        return await self._invoker.invoke(
            decision.capability_id,
            request.input,
            context,
            plugin_id=caller_plugin_id,
            deadline_at=context.deadline_at,
            parent=parent,
            trace_enabled=trace_enabled,
        )

    def _select_planner_id(self, routing_context: RoutingContext) -> str:
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
        return planner.planner_id

    def _with_route_metadata(
        self,
        result: ResultEnvelope,
        decision: RouteDecision,
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
        payload["metadata"] = metadata
        return ResultEnvelope.model_validate(payload)
