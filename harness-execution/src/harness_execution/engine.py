"""ExecutionPlan 的 Request 级执行与恢复入口。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityError,
    ExecutionPlan,
    HarnessTimeoutError,
    InvocationContext,
    NodeExecutionStatus,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    Request,
    RequestError,
    ResultEnvelope,
)
from harness_planning import PlanValidationError, PlanValidator
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_state import InMemoryStateStore, StateStore
from harness_trace import SpanType, Tracer

from .cancellation import CancellationSignal
from .recovery import ResumeCoordinator
from .scheduler import BasicScheduler


_TERMINAL_PLAN_STATUSES = {
    PlanExecutionStatus.SUCCEEDED,
    PlanExecutionStatus.PARTIAL,
    PlanExecutionStatus.FAILED,
    PlanExecutionStatus.DENIED,
    PlanExecutionStatus.CANCELLED,
}


class ExecutionEngine:
    """验证并执行/恢复 Plan，协调 REQUEST/RUNTIME/PLAN Trace 生命周期。"""

    def __init__(
        self,
        validator: PlanValidator,
        scheduler: BasicScheduler,
        invoker: CapabilityInvoker,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
        state_store: StateStore | None = None,
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
        self._validator = validator
        self._scheduler = scheduler
        self._invoker = invoker
        self._tracer = tracer
        self._lifecycle = lifecycle
        self._state_store = effective_state_store
        self._resume = ResumeCoordinator(scheduler)
        self._states: dict[str, PlanExecutionState] = {}
        # active 只保存当前进程内正在推进的 Plan。Signal 与序列化 State 分离，
        # 后续多进程取消需要由 StateStore/消息机制替换这一索引。
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

        signal = CancellationSignal()
        registered = False
        record_created = False
        checkpoint_lock = asyncio.Lock()

        async def checkpoint(state: PlanExecutionState) -> None:
            """把 Scheduler 状态包装成可恢复记录并保存为完整快照。"""

            nonlocal record_created
            # Retry Task 可能与其他并行节点几乎同时到达 checkpoint；串行化快照和
            # 写入可确保较旧 state_version 不会在较新版本之后覆盖数据库记录。
            async with checkpoint_lock:
                state_snapshot = self._snapshot(state)
                self._states[plan.plan_id] = state_snapshot
                record = PlanExecutionRecord(
                    plan_id=plan.plan_id,
                    plan=plan,
                    context=context.model_copy(update={"cancellation": signal.snapshot()}),
                    state=state_snapshot,
                )
                if record_created:
                    await self._state_store.save(record)
                else:
                    await self._state_store.create(record)
                    record_created = True

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
            outcome = await self._scheduler.run(
                request,
                plan,
                context,
                parent=plan_span,
                trace_enabled=trace_enabled,
                cancellation=signal,
                checkpoint=checkpoint,
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

        result = self._lifecycle.normalize_trace_id(result, request_span)
        self._lifecycle.finish_from_result(plan_span, result)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
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

    async def _resume_record(self, record: PlanExecutionRecord) -> ResultEnvelope:
        plan = record.plan
        context = record.context
        request = context.request
        trace_enabled = request.options.trace
        request_span = (
            self._lifecycle.start_request_span(context) if trace_enabled else None
        )
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
                "plan.resumed",
                attributes={"state_version": record.state_version},
            )

        signal = CancellationSignal.from_snapshot(record.context.cancellation)
        state = self._snapshot(record.state)
        registered = False
        checkpoint_lock = asyncio.Lock()

        async def checkpoint(current_state: PlanExecutionState) -> None:
            """Resume 只更新已经存在的记录，永远不 create 新 plan_id。"""

            async with checkpoint_lock:
                state_snapshot = self._snapshot(current_state)
                self._states[plan.plan_id] = state_snapshot
                await self._state_store.save(
                    PlanExecutionRecord(
                        plan_id=plan.plan_id,
                        plan=plan,
                        context=context.model_copy(
                            update={"cancellation": signal.snapshot()}
                        ),
                        state=state_snapshot,
                    )
                )

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
                state.status = PlanExecutionStatus.FAILED
                state.updated_at = now
                state.completed_at = now
                state.state_version += 1
                state.metadata["final_result"] = result.model_dump(mode="json")
                await checkpoint(state)
            else:
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
                self._states[plan.plan_id] = self._snapshot(outcome.state)
                result = outcome.result
        except asyncio.CancelledError:
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
        self._lifecycle.finish_from_result(plan_span, result)
        self._lifecycle.finish_from_result(runtime_span, result)
        self._lifecycle.finish_from_result(request_span, result)
        return result

    async def cancel(self, plan_id: str, reason: str | None = None) -> bool:
        """请求取消当前进程内正在执行的 Plan。

        返回值表示是否找到了活动 Plan 并首次写入取消信号；未知、已完成或已经请求
        取消的 Plan 返回 ``False``。
        """

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise TypeError("plan_id must be a non-empty string")
        async with self._active_lock:
            signal = self._active.get(plan_id)
            return signal.request(reason) if signal is not None else False

    def state(self, plan_id: str) -> PlanExecutionState | None:
        """返回本进程最近 checkpoint 的缓存副本；持久记录由 StateStore 持有。"""

        state = self._states.get(plan_id)
        return self._snapshot(state) if state is not None else None

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
