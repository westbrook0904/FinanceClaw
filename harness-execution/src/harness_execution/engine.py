"""ExecutionPlan 的 Request 级执行入口。"""

from __future__ import annotations

import asyncio

from harness_contracts import (
    CapabilityError,
    ExecutionPlan,
    PlanExecutionState,
    Request,
    RequestError,
    ResultEnvelope,
)
from harness_planning import PlanValidationError, PlanValidator
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_trace import SpanType, Tracer

from .scheduler import BasicScheduler


class ExecutionEngine:
    """验证并执行 Plan，协调 REQUEST/RUNTIME/PLAN Trace 生命周期。"""

    def __init__(
        self,
        validator: PlanValidator,
        scheduler: BasicScheduler,
        invoker: CapabilityInvoker,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
    ) -> None:
        if not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")
        if not isinstance(scheduler, BasicScheduler):
            raise TypeError("scheduler must be BasicScheduler")
        if not isinstance(invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if invoker.tracer is not tracer or invoker.lifecycle is not lifecycle:
            raise ValueError("engine components must share one tracer and lifecycle")
        self._validator = validator
        self._scheduler = scheduler
        self._invoker = invoker
        self._tracer = tracer
        self._lifecycle = lifecycle
        self._states: dict[str, PlanExecutionState] = {}

    @property
    def validator(self) -> PlanValidator:
        return self._validator

    @property
    def scheduler(self) -> BasicScheduler:
        return self._scheduler

    async def execute(self, request: Request, plan: ExecutionPlan) -> ResultEnvelope:
        """创建并推进一个 Plan，直到最终状态或明确 WAITING。"""

        context_result = self._lifecycle.create_context(request)
        if isinstance(context_result, ResultEnvelope):
            return context_result
        context = context_result
        trace_enabled = request.options.trace
        request_span = (
            self._lifecycle.start_request_span(context) if trace_enabled else None
        )
        runtime_span = (
            self._tracer.start_span(
                "runtime.execute_plan",
                SpanType.RUNTIME,
                parent=request_span,
                attributes={"request_id": request.request_id, "plan_id": plan.plan_id},
            )
            if trace_enabled
            else None
        )
        plan_span = (
            self._tracer.start_span(
                f"plan.{plan.plan_id}",
                SpanType.PLAN,
                parent=runtime_span,
                attributes={"plan_id": plan.plan_id, "plan_revision": plan.revision},
            )
            if trace_enabled
            else None
        )
        if plan_span is not None:
            context = self._lifecycle.with_trace_context(context, plan_span)

        try:
            self._validator.validate(plan)
            outcome = await self._scheduler.run(
                request,
                plan,
                context,
                parent=plan_span,
                trace_enabled=trace_enabled,
            )
            self._states[plan.plan_id] = self._snapshot(outcome.state)
            result = outcome.result
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(plan_span)
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise
        except PlanValidationError as exc:
            error = RequestError(
                "execution plan validation failed",
                code="HARNESS.PLAN.INVALID",
                details={
                    "plan_id": plan.plan_id,
                    "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                },
            )
            result = ResultEnvelope.failure(error.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "execution engine failed",
                code="HARNESS.PLAN.EXECUTION_FAILED",
                details={"plan_id": plan.plan_id, "cause_type": type(exc).__name__},
            )
            result = ResultEnvelope.failure(error.to_detail())

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._lifecycle.finish_from_result(plan_span, result)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result

    def state(self, plan_id: str) -> PlanExecutionState | None:
        """返回 Basic Scheduler 的内存状态快照；后续由 StateStore 替代。"""

        state = self._states.get(plan_id)
        return self._snapshot(state) if state is not None else None

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
