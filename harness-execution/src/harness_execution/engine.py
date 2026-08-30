"""ExecutionPlan 的 Request 级执行、恢复与外部完成入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    CapabilityError,
    ErrorCode,
    ExecutionPlan,
    HarnessTimeoutError,
    InvocationContext,
    JsonValue,
    NodeExecutionStatus,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PolicyError,
    Request,
    RequestError,
    ResultEnvelope,
)
from harness_events import EventPublisher, ExecutionEventName, NoOpEventPublisher
from harness_planning import PlanValidationError, PlanValidator
from harness_policy import PolicyContext, PolicyEffect, PolicyPhase
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_state import InMemoryStateStore, StateRecordExistsError, StateStore
from harness_trace import Span, SpanStatus, SpanType, Tracer

from .approval import ApprovalCoordinator
from .async_waiting import AsyncWaitingCoordinator
from .cancellation import CancellationSignal
from .eventing import EventSpec, ExecutionEventEmitter
from .recovery import ResumeCoordinator
from .scheduler import BasicScheduler, CheckpointError

_TERMINAL_PLAN_STATUSES = {
    PlanExecutionStatus.SUCCEEDED,
    PlanExecutionStatus.PARTIAL,
    PlanExecutionStatus.FAILED,
    PlanExecutionStatus.DENIED,
    PlanExecutionStatus.CANCELLED,
}
_APPROVAL_GRANTS_ATTRIBUTE = "_harness_approval_grants"


class ExecutionEngine:
    """验证并执行/恢复 Plan，协调 Policy、Trace、Events 与 StateStore。"""

    def __init__(
        self,
        validator: PlanValidator,
        scheduler: BasicScheduler,
        invoker: CapabilityInvoker,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
        state_store: StateStore | None = None,
        event_publisher: EventPublisher | None = None,
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
        effective_state_store = state_store or InMemoryStateStore()
        if not isinstance(effective_state_store, StateStore):
            raise TypeError("state_store must implement StateStore")
        effective_publisher = event_publisher or NoOpEventPublisher()
        if not isinstance(effective_publisher, EventPublisher):
            raise TypeError("event_publisher must implement EventPublisher")

        self._validator = validator
        self._scheduler = scheduler
        self._invoker = invoker
        self._tracer = tracer
        self._lifecycle = lifecycle
        self._state_store = effective_state_store
        self._events = ExecutionEventEmitter(effective_publisher)
        self._resume = ResumeCoordinator(scheduler)
        self._approval = ApprovalCoordinator(scheduler)
        self._async_waiting = AsyncWaitingCoordinator(scheduler)
        self._states: dict[str, PlanExecutionState] = {}
        self._active: dict[str, CancellationSignal] = {}
        self._active_lock = asyncio.Lock()

    @property
    def validator(self) -> PlanValidator:
        return self._validator

    @property
    def scheduler(self) -> BasicScheduler:
        return self._scheduler

    @property
    def state_store(self) -> StateStore:
        return self._state_store

    @property
    def event_publisher(self) -> EventPublisher:
        return self._events.publisher

    async def execute(self, request: Request, plan: ExecutionPlan) -> ResultEnvelope:
        """创建并推进一个 Plan，直到最终状态或明确 WAITING。"""

        context_result = self._lifecycle.create_context(request)
        if isinstance(context_result, ResultEnvelope):
            return context_result
        context = context_result
        trace_enabled = request.options.trace
        request_span = self._lifecycle.start_request_span(context) if trace_enabled else None
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
        if runtime_span is not None:
            context = self._lifecycle.with_trace_context(context, runtime_span)

        try:
            result = await self.execute_with_context(
                request,
                plan,
                context,
                parent=runtime_span,
            )
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result

    async def execute_with_context(
        self,
        request: Request,
        plan: ExecutionPlan,
        context: InvocationContext,
        *,
        parent: Span | None,
    ) -> ResultEnvelope:
        """在调用方已有的 Request 生命周期内验证并推进一个 Plan。"""

        if not isinstance(request, Request):
            raise TypeError("request must be Request")
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be ExecutionPlan")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")
        if parent is not None and not isinstance(parent, Span):
            raise TypeError("parent must be Span or None")
        if context.request != request:
            error = RequestError(
                "invocation context belongs to another request",
                code="HARNESS.REQUEST.CONTEXT_MISMATCH",
            )
            return ResultEnvelope.failure(error.to_detail())

        context = self._strip_reserved_attributes(context)
        trace_enabled = request.options.trace
        plan_span = (
            self._tracer.start_span(
                f"plan.{plan.plan_id}",
                SpanType.PLAN,
                parent=parent,
                attributes={"plan_id": plan.plan_id, "plan_revision": plan.revision},
            )
            if trace_enabled
            else None
        )
        if plan_span is not None:
            context = self._lifecycle.with_trace_context(context, plan_span)

        signal = CancellationSignal()
        registered = False
        record_created = False
        checkpoint_lock = asyncio.Lock()
        last_checkpoint: PlanExecutionState | None = None
        scheduler_span: Span | None = None

        async def checkpoint(state: PlanExecutionState) -> None:
            """把 Scheduler 状态保存成完整快照，再发布 best-effort 执行事件。"""

            nonlocal record_created, last_checkpoint
            async with checkpoint_lock:
                state_snapshot = self._snapshot(state)
                previous = last_checkpoint
                self._states[plan.plan_id] = state_snapshot
                record = PlanExecutionRecord(
                    plan_id=plan.plan_id,
                    plan=plan,
                    context=self._checkpoint_context(context, signal),
                    state=state_snapshot,
                )
                if record_created:
                    await self._state_store.save(record)
                else:
                    await self._state_store.create(record)
                    record_created = True
                specs = await self._events.emit_checkpoint(
                    previous,
                    state_snapshot,
                    trace_id=self._trace_id(context, plan_span),
                )
                self._trace_specs(scheduler_span or plan_span, specs, state_snapshot.state_version)
                last_checkpoint = state_snapshot

        try:
            async with self._active_lock:
                if plan.plan_id in self._active:
                    raise RequestError(
                        "execution plan is already running",
                        code="HARNESS.PLAN.ALREADY_RUNNING",
                        details={"plan_id": plan.plan_id},
                    )
                self._active[plan.plan_id] = signal
                registered = True

            self._validator.validate(plan)
            pre_plan_result = self._evaluate_pre_plan(
                plan,
                context,
                parent=plan_span,
                trace_enabled=trace_enabled,
            )
            if pre_plan_result is not None:
                result = pre_plan_result
            else:
                scheduler_span = self._start_scheduler_span(
                    plan,
                    plan_span,
                    trace_enabled=trace_enabled,
                    resumed=False,
                )
                outcome = await self._scheduler.run(
                    request,
                    plan,
                    context,
                    parent=plan_span,
                    trace_enabled=trace_enabled,
                    cancellation=signal,
                    checkpoint=checkpoint,
                )
                state = self._snapshot(outcome.state)
                materialized_approvals, materialized_jobs = self._materialize_waiting(plan, state)
                if materialized_approvals or materialized_jobs:
                    await checkpoint(state)
                await self._emit_materialized_waiting(
                    plan,
                    state,
                    materialized_approvals,
                    materialized_jobs,
                    span=scheduler_span or plan_span,
                    trace_id=self._trace_id(context, plan_span),
                    recovered=False,
                )
                self._states[plan.plan_id] = self._snapshot(state)
                result = self._approval.refresh_accepted_result(outcome.result, state)
                result = self._async_waiting.refresh_accepted_result(result, state)
        except asyncio.CancelledError:
            self._finish_cancelled_if_running(scheduler_span)
            self._lifecycle.finish_cancelled(plan_span)
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
        except CheckpointError as exc:
            if isinstance(exc.__cause__, StateRecordExistsError):
                error = RequestError(
                    "execution plan identity already exists",
                    code=ErrorCode.PLAN_EXECUTION_ID_CONFLICT,
                    details={"plan_id": plan.plan_id},
                )
            else:
                error = CapabilityError(
                    "execution engine failed",
                    code="HARNESS.PLAN.EXECUTION_FAILED",
                    details={"plan_id": plan.plan_id, "cause_type": type(exc).__name__},
                )
            result = ResultEnvelope.failure(error.to_detail())
        except RequestError as exc:
            result = ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "execution engine failed",
                code="HARNESS.PLAN.EXECUTION_FAILED",
                details={"plan_id": plan.plan_id, "cause_type": type(exc).__name__},
            )
            result = ResultEnvelope.failure(error.to_detail())
        finally:
            if registered:
                async with self._active_lock:
                    if self._active.get(plan.plan_id) is signal:
                        del self._active[plan.plan_id]

        self._finish_span_from_result_if_running(scheduler_span, result)
        self._lifecycle.finish_from_result(plan_span, result)
        return result

    async def resume(self, plan_id: str) -> ResultEnvelope:
        """从 StateStore 加载同一个 plan_id 的 checkpoint 并继续推进。"""

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise TypeError("plan_id must be a non-empty string")
        try:
            record = await self._state_store.load(plan_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = RequestError(
                "failed to load plan execution state",
                code="HARNESS.PLAN.STATE_LOAD_FAILED",
                details={"plan_id": plan_id, "cause_type": type(exc).__name__},
            )
            return ResultEnvelope.failure(error.to_detail())
        if record is None:
            error = RequestError(
                "plan execution state was not found",
                code="HARNESS.PLAN.NOT_FOUND",
                details={"plan_id": plan_id},
            )
            return ResultEnvelope.failure(error.to_detail())
        return await self._resume_record(record)

    async def resolve_approval(
        self,
        plan_id: str,
        decision: ApprovalDecision,
    ) -> ResultEnvelope:
        """持久化 ApprovalDecision，并复用 Resume 继续同一个 Plan。"""

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise TypeError("plan_id must be a non-empty string")
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("decision must be ApprovalDecision")

        try:
            record = await self._state_store.load(plan_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = RequestError(
                "failed to load plan execution state for approval",
                code="HARNESS.APPROVAL.STATE_LOAD_FAILED",
                details={"plan_id": plan_id, "cause_type": type(exc).__name__},
            )
            return ResultEnvelope.failure(error.to_detail())
        if record is None:
            error = RequestError(
                "plan execution state was not found",
                code="HARNESS.PLAN.NOT_FOUND",
                details={"plan_id": plan_id},
            )
            return ResultEnvelope.failure(error.to_detail())

        pending_request = next(
            (
                item
                for item in record.state.pending_approvals
                if item.approval_id == decision.approval_id
            ),
            None,
        )
        try:
            async with self._active_lock:
                if plan_id in self._active:
                    raise RequestError(
                        "execution plan is already running",
                        code="HARNESS.PLAN.ALREADY_RUNNING",
                        details={"plan_id": plan_id},
                    )
            self._validator.validate(record.plan)
            self._resume.validate(record.plan, record.state)
            updated = self._approval.resolve(record, decision)
            await self._state_store.save(updated)
            self._states[plan_id] = self._snapshot(updated.state)
            trace_id = self._stored_trace_id(record.context)
            await self._emit_external_checkpoint(record.state, updated.state, trace_id=trace_id)
            node_id = pending_request.node_id if pending_request is not None else None
            await self._events.emit(
                ExecutionEventName.APPROVAL_RESOLVED,
                plan_id=plan_id,
                node_id=node_id,
                state_version=updated.state_version,
                trace_id=trace_id,
                attributes={
                    "approval_id": decision.approval_id,
                    "decision": decision.decision.value,
                    "decided_by": decision.decided_by,
                },
            )
        except asyncio.CancelledError:
            raise
        except PlanValidationError as exc:
            error = RequestError(
                "stored execution plan validation failed",
                code="HARNESS.PLAN.INVALID",
                details={
                    "plan_id": plan_id,
                    "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                },
            )
            return ResultEnvelope.failure(error.to_detail())
        except RequestError as exc:
            return ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "failed to persist approval decision",
                code="HARNESS.APPROVAL.RESOLUTION_FAILED",
                details={"plan_id": plan_id, "cause_type": type(exc).__name__},
            )
            return ResultEnvelope.failure(error.to_detail())

        resumed_nodes = ()
        if (
            pending_request is not None
            and decision.decision is ApprovalDecisionType.APPROVED
            and updated.state.nodes[pending_request.node_id].status is NodeExecutionStatus.READY
        ):
            resumed_nodes = (pending_request.node_id,)
        return await self._resume_record(
            updated,
            trigger_name=ExecutionEventName.APPROVAL_RESOLVED.value,
            trigger_attributes={
                "approval_id": decision.approval_id,
                "decision": decision.decision.value,
                **({"node_id": pending_request.node_id} if pending_request is not None else {}),
            },
            resumed_nodes=resumed_nodes,
        )

    async def complete_async_node(
        self,
        plan_id: str,
        node_id: str,
        terminal_result: ResultEnvelope,
    ) -> ResultEnvelope:
        """持久化异步 Capability 的终态结果，并继续推进同一个 Plan。"""

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise TypeError("plan_id must be a non-empty string")
        if not isinstance(node_id, str) or not node_id.strip():
            raise TypeError("node_id must be a non-empty string")
        if not isinstance(terminal_result, ResultEnvelope):
            raise TypeError("terminal_result must be ResultEnvelope")

        try:
            record = await self._state_store.load(plan_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = RequestError(
                "failed to load plan execution state for async completion",
                code="HARNESS.ASYNC.STATE_LOAD_FAILED",
                details={"plan_id": plan_id, "cause_type": type(exc).__name__},
            )
            return ResultEnvelope.failure(error.to_detail())
        if record is None:
            error = RequestError(
                "plan execution state was not found",
                code="HARNESS.PLAN.NOT_FOUND",
                details={"plan_id": plan_id},
            )
            return ResultEnvelope.failure(error.to_detail())

        pending = next(
            (item for item in record.state.pending_jobs if item.node_id == node_id),
            None,
        )
        try:
            async with self._active_lock:
                if plan_id in self._active:
                    raise RequestError(
                        "execution plan is already running",
                        code="HARNESS.PLAN.ALREADY_RUNNING",
                        details={"plan_id": plan_id},
                    )
            self._validator.validate(record.plan)
            self._resume.validate(record.plan, record.state)
            updated = self._async_waiting.resolve(record, node_id, terminal_result)
            await self._state_store.save(updated)
            self._states[plan_id] = self._snapshot(updated.state)
            trace_id = self._stored_trace_id(record.context)
            await self._emit_external_checkpoint(record.state, updated.state, trace_id=trace_id)
            await self._events.emit(
                ExecutionEventName.ASYNC_COMPLETED,
                plan_id=plan_id,
                node_id=node_id,
                state_version=updated.state_version,
                trace_id=trace_id,
                attributes={
                    "status": terminal_result.status.value,
                    **({"job_ref": pending.job_ref} if pending is not None else {}),
                },
            )
        except asyncio.CancelledError:
            raise
        except PlanValidationError as exc:
            error = RequestError(
                "stored execution plan validation failed",
                code="HARNESS.PLAN.INVALID",
                details={
                    "plan_id": plan_id,
                    "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                },
            )
            return ResultEnvelope.failure(error.to_detail())
        except RequestError as exc:
            return ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "failed to persist async completion",
                code="HARNESS.ASYNC.COMPLETION_FAILED",
                details={
                    "plan_id": plan_id,
                    "node_id": node_id,
                    "cause_type": type(exc).__name__,
                },
            )
            return ResultEnvelope.failure(error.to_detail())

        return await self._resume_record(
            updated,
            trigger_name=ExecutionEventName.ASYNC_COMPLETED.value,
            trigger_attributes={
                "node_id": node_id,
                "status": terminal_result.status.value,
                **({"job_ref": pending.job_ref} if pending is not None else {}),
            },
        )

    async def _resume_record(
        self,
        record: PlanExecutionRecord,
        *,
        trigger_name: str | None = None,
        trigger_attributes: dict[str, JsonValue] | None = None,
        resumed_nodes: tuple[str, ...] = (),
    ) -> ResultEnvelope:
        plan = record.plan
        state = self._snapshot(record.state)
        context = self._context_with_approval_grants(record.context, state)
        request = context.request
        trace_enabled = request.options.trace
        request_span = self._lifecycle.start_request_span(context) if trace_enabled else None
        runtime_span = (
            self._tracer.start_span(
                "runtime.resume_plan",
                SpanType.RUNTIME,
                parent=request_span,
                attributes={"request_id": request.request_id, "plan_id": plan.plan_id},
            )
            if trace_enabled
            else None
        )
        plan_span = (
            self._tracer.start_span(
                f"plan.{plan.plan_id}.resume",
                SpanType.PLAN,
                parent=runtime_span,
                attributes={
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.revision,
                    "resumed": True,
                    "resume_state_version": record.state_version,
                },
            )
            if trace_enabled
            else None
        )
        if plan_span is not None:
            context = self._lifecycle.with_trace_context(context, plan_span)
            self._tracer.add_event(
                plan_span,
                ExecutionEventName.PLAN_RESUMED.value,
                attributes={"state_version": record.state_version},
            )
            if trigger_name is not None:
                self._tracer.add_event(
                    plan_span,
                    trigger_name,
                    attributes=trigger_attributes or {},
                )

        trace_id = self._trace_id(context, plan_span)
        await self._events.emit(
            ExecutionEventName.PLAN_RESUMED,
            plan_id=plan.plan_id,
            state_version=state.state_version,
            trace_id=trace_id,
            attributes={"from_state_version": record.state_version},
        )

        interrupted_nodes = tuple(
            node_id
            for node_id, node_state in state.nodes.items()
            if node_state.status is NodeExecutionStatus.RUNNING
            or (node_state.status is NodeExecutionStatus.READY and node_state.attempt > 0)
        )
        node_resumes = tuple(dict.fromkeys((*resumed_nodes, *interrupted_nodes)))
        for node_id in node_resumes:
            await self._events.emit(
                ExecutionEventName.NODE_RESUMED,
                plan_id=plan.plan_id,
                node_id=node_id,
                state_version=state.state_version,
                trace_id=trace_id,
                attributes={"attempt": state.nodes[node_id].attempt},
            )
            if plan_span is not None:
                self._tracer.add_event(
                    plan_span,
                    ExecutionEventName.NODE_RESUMED.value,
                    attributes={"node_id": node_id, "attempt": state.nodes[node_id].attempt},
                )

        signal = CancellationSignal.from_snapshot(record.context.cancellation)
        registered = False
        checkpoint_lock = asyncio.Lock()
        last_checkpoint = self._snapshot(state)
        scheduler_span: Span | None = None

        async def checkpoint(current_state: PlanExecutionState) -> None:
            """Resume 只更新已经存在的记录，并派生稳定执行事件。"""

            nonlocal last_checkpoint
            async with checkpoint_lock:
                state_snapshot = self._snapshot(current_state)
                previous = last_checkpoint
                self._states[plan.plan_id] = state_snapshot
                await self._state_store.save(
                    PlanExecutionRecord(
                        plan_id=plan.plan_id,
                        plan=plan,
                        context=self._checkpoint_context(context, signal),
                        state=state_snapshot,
                    )
                )
                specs = await self._events.emit_checkpoint(
                    previous,
                    state_snapshot,
                    trace_id=trace_id,
                )
                self._trace_specs(scheduler_span or plan_span, specs, state_snapshot.state_version)
                last_checkpoint = state_snapshot

        try:
            async with self._active_lock:
                if plan.plan_id in self._active:
                    raise RequestError(
                        "execution plan is already running",
                        code="HARNESS.PLAN.ALREADY_RUNNING",
                        details={"plan_id": plan.plan_id},
                    )
                self._active[plan.plan_id] = signal
                registered = True

            self._validator.validate(plan)
            self._resume.validate(plan, state)
            materialized_approvals, materialized_jobs = self._materialize_waiting(plan, state)
            if materialized_approvals or materialized_jobs:
                await checkpoint(state)
            await self._emit_materialized_waiting(
                plan,
                state,
                materialized_approvals,
                materialized_jobs,
                span=plan_span,
                trace_id=trace_id,
                recovered=True,
            )
            context = self._context_with_approval_grants(context, state)

            stored_result = (
                self._stored_terminal_result(state)
                if state.status in _TERMINAL_PLAN_STATUSES
                else None
            )
            if stored_result is not None:
                result = stored_result
            elif (
                state.status not in _TERMINAL_PLAN_STATUSES
                and not signal.cancelled
                and self._plan_deadline_expired(plan, context)
            ):
                error = HarnessTimeoutError(
                    "execution plan deadline exceeded before resume",
                    details={"plan_id": plan.plan_id},
                )
                result = ResultEnvelope.failure(
                    error.to_detail(),
                    metadata={"plan_id": plan.plan_id, "plan_revision": plan.revision},
                )
                now = datetime.now(UTC)
                for node_state in state.nodes.values():
                    if node_state.status in {
                        NodeExecutionStatus.PENDING,
                        NodeExecutionStatus.READY,
                        NodeExecutionStatus.RUNNING,
                        NodeExecutionStatus.WAITING,
                    }:
                        node_state.status = NodeExecutionStatus.CANCELLED
                        node_state.completed_at = now
                        node_state.result = None
                        node_state.waiting_reason = None
                        node_state.continuation = None
                state.pending_approvals.clear()
                state.pending_jobs.clear()
                state.status = PlanExecutionStatus.FAILED
                state.updated_at = now
                state.completed_at = now
                state.state_version += 1
                state.metadata["final_result"] = result.model_dump(mode="json")
                await checkpoint(state)
            else:
                scheduler_span = self._start_scheduler_span(
                    plan,
                    plan_span,
                    trace_enabled=trace_enabled,
                    resumed=True,
                )
                outcome = await self._resume.run(
                    request,
                    plan,
                    context,
                    state,
                    parent=plan_span,
                    trace_enabled=trace_enabled,
                    cancellation=signal,
                    checkpoint=checkpoint,
                )
                state = self._snapshot(outcome.state)
                materialized_approvals, materialized_jobs = self._materialize_waiting(plan, state)
                if materialized_approvals or materialized_jobs:
                    await checkpoint(state)
                await self._emit_materialized_waiting(
                    plan,
                    state,
                    materialized_approvals,
                    materialized_jobs,
                    span=scheduler_span or plan_span,
                    trace_id=trace_id,
                    recovered=True,
                )
                self._states[plan.plan_id] = self._snapshot(state)
                result = self._approval.refresh_accepted_result(outcome.result, state)
                result = self._async_waiting.refresh_accepted_result(result, state)
        except asyncio.CancelledError:
            self._finish_cancelled_if_running(scheduler_span)
            self._lifecycle.finish_cancelled(plan_span)
            self._lifecycle.finish_cancelled(runtime_span)
            self._lifecycle.finish_cancelled(request_span)
            raise
        except PlanValidationError as exc:
            error = RequestError(
                "stored execution plan validation failed",
                code="HARNESS.PLAN.INVALID",
                details={
                    "plan_id": plan.plan_id,
                    "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                },
            )
            result = ResultEnvelope.failure(error.to_detail())
        except RequestError as exc:
            result = ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "execution plan resume failed",
                code="HARNESS.PLAN.RESUME_FAILED",
                details={"plan_id": plan.plan_id, "cause_type": type(exc).__name__},
            )
            result = ResultEnvelope.failure(error.to_detail())
        finally:
            if registered:
                async with self._active_lock:
                    if self._active.get(plan.plan_id) is signal:
                        del self._active[plan.plan_id]

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._finish_span_from_result_if_running(scheduler_span, result)
        self._lifecycle.finish_from_result(plan_span, result)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result

    async def cancel(self, plan_id: str, reason: str | None = None) -> bool:
        """请求取消当前进程内正在执行的 Plan。"""

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise TypeError("plan_id must be a non-empty string")
        async with self._active_lock:
            signal = self._active.get(plan_id)
            return signal.request(reason) if signal is not None else False

    def state(self, plan_id: str) -> PlanExecutionState | None:
        """返回本进程最近 checkpoint 的缓存副本；持久记录由 StateStore 持有。"""

        state = self._states.get(plan_id)
        return self._snapshot(state) if state is not None else None

    def _evaluate_pre_plan(
        self,
        plan: ExecutionPlan,
        context: InvocationContext,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope | None:
        span = (
            self._tracer.start_span(
                "policy.pre_plan",
                SpanType.POLICY,
                parent=parent,
                attributes={"plan_id": plan.plan_id, "node_count": len(plan.nodes)},
            )
            if trace_enabled
            else None
        )
        policy_context = PolicyContext(
            invocation=(
                self._lifecycle.with_trace_context(context, span) if span is not None else context
            ),
            phase=PolicyPhase.PRE_PLAN,
            plan=plan,
        )
        try:
            decision = self._invoker.policy_engine.evaluate(policy_context)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, PolicyError)
                else PolicyError(
                    "pre-plan policy evaluation failed",
                    code="HARNESS.POLICY.EVALUATION_FAILED",
                    details={"plan_id": plan.plan_id, "cause_type": type(exc).__name__},
                )
            )
            self._lifecycle.finish_error(span, error)
            if error is exc:
                raise
            raise error from exc

        self._lifecycle.finish_ok(
            span,
            attributes={"effect": decision.effect.value, "policy": decision.policy},
        )
        constraints = decision.model_dump(mode="json")["constraints"]
        if decision.effect is PolicyEffect.DENY:
            error = PolicyError(
                decision.reason or "policy denied execution plan",
                code="HARNESS.POLICY.PLAN_DENIED",
                details={"policy": decision.policy, "constraints": constraints},
            )
            return ResultEnvelope.denied(error.to_detail())
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            error = PolicyError(
                "pre-plan approval is not a stage-two waiting boundary",
                code="HARNESS.POLICY.PLAN_APPROVAL_UNSUPPORTED",
                details={"policy": decision.policy, "constraints": constraints},
            )
            return ResultEnvelope.denied(error.to_detail())
        return None

    def _materialize_waiting(self, plan: ExecutionPlan, state: PlanExecutionState):
        approvals = self._approval.ensure_waiting_requests(plan, state)
        jobs = self._async_waiting.ensure_waiting_jobs(plan, state)
        return approvals, jobs

    async def _emit_materialized_waiting(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
        approvals,
        jobs,
        *,
        span: Span | None,
        trace_id: str | None,
        recovered: bool,
    ) -> None:
        for approval in approvals:
            attributes: dict[str, JsonValue] = {
                "approval_id": approval.approval_id,
                "source": approval.metadata.get("source", "approval"),
            }
            if approval.capability is not None:
                attributes["capability"] = approval.capability
            if recovered:
                attributes["recovered"] = True
            await self._events.emit(
                ExecutionEventName.APPROVAL_REQUESTED,
                plan_id=plan.plan_id,
                node_id=approval.node_id,
                state_version=state.state_version,
                trace_id=trace_id,
                attributes=attributes,
            )
            self._trace_event(
                span,
                ExecutionEventName.APPROVAL_REQUESTED.value,
                {"node_id": approval.node_id, **attributes},
            )
        for job in jobs:
            if job.node_id is None or job.job_ref is None:
                continue
            attributes = {"job_ref": job.job_ref}
            if recovered:
                attributes["recovered"] = True
            await self._events.emit(
                ExecutionEventName.ASYNC_ACCEPTED,
                plan_id=plan.plan_id,
                node_id=job.node_id,
                state_version=state.state_version,
                trace_id=trace_id,
                attributes=attributes,
            )
            self._trace_event(
                span,
                ExecutionEventName.ASYNC_ACCEPTED.value,
                {"node_id": job.node_id, **attributes},
            )

    async def _emit_external_checkpoint(
        self,
        previous: PlanExecutionState,
        current: PlanExecutionState,
        *,
        trace_id: str | None,
    ) -> None:
        await self._events.emit_checkpoint(previous, current, trace_id=trace_id)

    def _context_with_approval_grants(
        self,
        context: InvocationContext,
        state: PlanExecutionState,
    ) -> InvocationContext:
        attributes = self._context_attributes(context)
        attributes.pop(_APPROVAL_GRANTS_ATTRIBUTE, None)
        grants = self._approval.grants(state)
        if grants:
            attributes[_APPROVAL_GRANTS_ATTRIBUTE] = [
                grant.model_dump(mode="json") for grant in grants
            ]
        return context.model_copy(update={"attributes": attributes})

    def _checkpoint_context(
        self,
        context: InvocationContext,
        signal: CancellationSignal,
    ) -> InvocationContext:
        clean = self._strip_reserved_attributes(context)
        return clean.model_copy(update={"cancellation": signal.snapshot()})

    @staticmethod
    def _strip_reserved_attributes(context: InvocationContext) -> InvocationContext:
        attributes = ExecutionEngine._context_attributes(context)
        attributes.pop(_APPROVAL_GRANTS_ATTRIBUTE, None)
        return context.model_copy(update={"attributes": attributes})

    @staticmethod
    def _context_attributes(context: InvocationContext) -> dict[str, JsonValue]:
        payload = context.model_dump(mode="json")["attributes"]
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _start_scheduler_span(
        self,
        plan: ExecutionPlan,
        parent: Span | None,
        *,
        trace_enabled: bool,
        resumed: bool,
    ) -> Span | None:
        if not trace_enabled:
            return None
        return self._tracer.start_span(
            "scheduler.resume" if resumed else "scheduler.execute",
            SpanType.SCHEDULER,
            parent=parent,
            attributes={
                "plan_id": plan.plan_id,
                "plan_revision": plan.revision,
                "resumed": resumed,
            },
        )

    def _trace_specs(
        self,
        span: Span | None,
        specs: tuple[EventSpec, ...],
        state_version: int,
    ) -> None:
        for spec in specs:
            attributes: dict[str, JsonValue] = {
                "state_version": state_version,
                **(spec.attributes or {}),
            }
            if spec.node_id is not None:
                attributes["node_id"] = spec.node_id
            self._trace_event(span, spec.name.value, attributes)

    def _trace_event(
        self,
        span: Span | None,
        name: str,
        attributes: dict[str, JsonValue],
    ) -> None:
        if span is None:
            return
        try:
            self._tracer.add_event(span, name, attributes=attributes)
        except Exception:
            # Trace 是观测面，不应因 exporter/生命周期竞争改变执行状态。
            return

    def _finish_span_from_result_if_running(
        self,
        span: Span | None,
        result: ResultEnvelope,
    ) -> None:
        if not self._span_running(span):
            return
        self._lifecycle.finish_from_result(span, result)

    def _finish_cancelled_if_running(self, span: Span | None) -> None:
        if self._span_running(span):
            self._lifecycle.finish_cancelled(span)

    def _span_running(self, span: Span | None) -> bool:
        if span is None:
            return False
        try:
            get_span = getattr(self._tracer, "get_span", None)
            current = get_span(span.span_id) if callable(get_span) else span
            return current is None or current.status is SpanStatus.RUNNING
        except Exception:
            return True

    @staticmethod
    def _trace_id(context: InvocationContext, span: Span | None) -> str | None:
        if span is not None:
            return span.trace_id
        return ExecutionEngine._stored_trace_id(context)

    @staticmethod
    def _stored_trace_id(context: InvocationContext) -> str | None:
        return context.trace_context.trace_id if context.trace_context is not None else None

    @staticmethod
    def _plan_deadline_expired(
        plan: ExecutionPlan,
        context: InvocationContext,
    ) -> bool:
        deadlines = [item for item in (context.deadline_at, plan.budget.deadline_at) if item]
        return bool(deadlines) and min(deadlines) <= datetime.now(UTC)

    @staticmethod
    def _stored_terminal_result(state: PlanExecutionState) -> ResultEnvelope | None:
        payload = state.metadata.get("final_result")
        if payload is None:
            return None
        try:
            return ResultEnvelope.model_validate(payload)
        except Exception as exc:
            raise RequestError(
                "stored final result is invalid",
                code="HARNESS.PLAN.STATE_INVALID",
                details={"plan_id": state.plan_id},
            ) from exc

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
