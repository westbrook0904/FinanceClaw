"""ExecutionPlan checkpoint 的跨进程恢复协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from harness_contracts import (
    CapabilityError,
    Continuation,
    ExecutionPlan,
    FailurePolicy,
    IdempotencyType,
    InvocationContext,
    NodeExecutionState,
    NodeExecutionStatus,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    ProviderAttempt,
    Request,
    RequestError,
    ResultEnvelope,
    ResultStatus,
    SideEffectType,
)
from harness_trace import Span, SpanType

from .cancellation import CancellationSignal
from .resolution import BindingResolutionError
from .scheduler import BasicScheduler, CheckpointError, SchedulerOutcome

type CheckpointCallback = Callable[[PlanExecutionState], Awaitable[None]]
type AttemptCheckpointCallback = Callable[[int], Awaitable[None]]

_TERMINAL_PLAN_STATUSES = {
    PlanExecutionStatus.SUCCEEDED,
    PlanExecutionStatus.PARTIAL,
    PlanExecutionStatus.FAILED,
    PlanExecutionStatus.DENIED,
    PlanExecutionStatus.CANCELLED,
}

_EXPECTED_RESULT_STATUSES = {
    NodeExecutionStatus.SUCCEEDED: {ResultStatus.SUCCESS, ResultStatus.PARTIAL},
    NodeExecutionStatus.FAILED: {ResultStatus.FAILED},
    NodeExecutionStatus.DENIED: {ResultStatus.DENIED},
    NodeExecutionStatus.WAITING: {ResultStatus.ACCEPTED},
}


class ResumeCoordinator:
    """从持久化 PlanExecutionState 继续推进同一个 ExecutionPlan。

    StateStore 只保存快照，不解释状态迁移；Resume 的恢复规则因此属于 execution
    状态机。协调器复用 BasicScheduler 的 DAG、结果合成和 CapabilityInvoker 路径，
    但不会把已完成节点重新执行。
    """

    def __init__(self, scheduler: BasicScheduler) -> None:
        if not isinstance(scheduler, BasicScheduler):
            raise TypeError("scheduler must be BasicScheduler")
        self._scheduler = scheduler

    def validate(self, plan: ExecutionPlan, state: PlanExecutionState) -> None:
        """验证持久化状态与静态 Plan 的恢复一致性，不修改传入对象。"""

        self._validate_state(plan, state)

    async def run(
        self,
        request: Request,
        plan: ExecutionPlan,
        context: InvocationContext,
        resume_state: PlanExecutionState,
        *,
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationSignal,
        checkpoint: CheckpointCallback,
    ) -> SchedulerOutcome:
        """恢复快照并推进，直到终态或明确 WAITING。"""

        state = self._snapshot(resume_state)
        self._validate_state(plan, state)
        results = {
            node_id: node_state.result
            for node_id, node_state in state.nodes.items()
            if node_state.result is not None
        }
        abort_result = self._restore_abort_result(plan, state)

        # 已经终态的 Plan resume 是幂等读取，不重新执行 Provider，也不制造新版本。
        if state.status in _TERMINAL_PLAN_STATUSES:
            result = self._scheduler._compose_result(  # noqa: SLF001
                plan,
                state,
                results,
                abort_result,
                cancellation,
            )
            return SchedulerOutcome(result=result, state=state)

        nodes = {node.node_id: node for node in plan.nodes}
        node_order = {node.node_id: index for index, node in enumerate(plan.nodes)}
        incoming = {node_id: [] for node_id in nodes}
        for edge in plan.edges:
            incoming[edge.to_node].append(edge)

        # RUNNING 表示进程可能在 Provider 调用期间退出。重放前必须通过与 Retry
        # 相同的幂等规则；READY + attempt>0 则表示上一次 resume 已完成恢复 checkpoint
        # 但尚未来得及重新创建 Task，同样属于中断中的调用尝试。
        replay_attempts: dict[str, int] = {}
        state_changed = False
        for node in plan.nodes:
            node_state = state.nodes[node.node_id]
            if node_state.status is NodeExecutionStatus.RUNNING or (
                node_state.status is NodeExecutionStatus.READY and node_state.attempt > 0
            ):
                if not self._replay_safe(node):
                    raise RequestError(
                        "interrupted capability cannot be safely replayed",
                        code="HARNESS.PLAN.RESUME_UNSAFE",
                        details={
                            "plan_id": plan.plan_id,
                            "node_id": node.node_id,
                            "attempt": node_state.attempt,
                        },
                    )
                replay_attempts[node.node_id] = max(node_state.attempt, 1)
                node_state.status = NodeExecutionStatus.READY
                node_state.completed_at = None
                node_state.error = None
                node_state.result = None
                state_changed = True

        # CREATED/RUNNING 快照和实际可继续执行的 WAITING 快照重新进入 RUNNING。
        # 纯 WAITING 且没有外部 completion 的快照保持 WAITING，resume 会幂等返回
        # ACCEPTED；Approval/Async 后续步骤只需先更新 waiting node 再调用本入口。
        has_waiting = any(
            item.status is NodeExecutionStatus.WAITING for item in state.nodes.values()
        )
        runnable = any(
            item.status in {NodeExecutionStatus.READY, NodeExecutionStatus.RUNNING}
            for item in state.nodes.values()
        ) or (
            any(item.status is NodeExecutionStatus.PENDING for item in state.nodes.values())
            and (
                state.status in {PlanExecutionStatus.CREATED, PlanExecutionStatus.RUNNING}
                or not has_waiting
            )
        )
        if runnable and state.status is not PlanExecutionStatus.RUNNING:
            state.status = PlanExecutionStatus.RUNNING
            state.completed_at = None
            state_changed = True

        if state_changed:
            self._scheduler._touch(state)  # noqa: SLF001
            await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001

        running: dict[asyncio.Task[tuple[str, ResultEnvelope, int]], tuple[str, int]] = {}
        cancellation_waiter = asyncio.create_task(cancellation.wait())

        try:
            while True:
                if cancellation.cancelled:
                    await self._scheduler._cancel_running(  # noqa: SLF001
                        {task: node_id for task, (node_id, _) in running.items()},
                        state,
                    )
                    running.clear()
                    self._scheduler._cancel_unstarted(state)  # noqa: SLF001
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
                    break

                # 若进程恰好在 fail-fast 节点 terminal checkpoint 之后退出，恢复时
                # 必须先完成原本尚未落下的 fail-fast 清理，不能继续启动其他节点。
                if abort_result is not None:
                    self._scheduler._cancel_unstarted(state)  # noqa: SLF001
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
                    break

                self._scheduler._advance_pending(state, incoming, results)  # noqa: SLF001
                ready = sorted(
                    (
                        node_id
                        for node_id, node_state in state.nodes.items()
                        if node_state.status is NodeExecutionStatus.READY
                    ),
                    key=node_order.__getitem__,
                )
                available = plan.budget.max_concurrency - len(running)
                for node_id in ready[:available]:
                    node = nodes[node_id]
                    node_state = state.nodes[node_id]
                    start_attempt = replay_attempts.pop(node_id, 1)
                    node_state.status = NodeExecutionStatus.RUNNING
                    node_state.attempt = start_attempt
                    if node_state.started_at is None:
                        node_state.started_at = datetime.now(UTC)
                    self._scheduler._touch(state)  # noqa: SLF001
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001

                    async def checkpoint_attempt(
                        attempt: int,
                        *,
                        current_node_id: str = node_id,
                    ) -> None:
                        state.nodes[current_node_id].attempt = attempt
                        self._scheduler._touch(state)  # noqa: SLF001
                        await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001

                    task = asyncio.create_task(
                        self._execute_node(
                            request,
                            plan,
                            node,
                            node_state,
                            context,
                            results,
                            start_attempt=start_attempt,
                            parent=parent,
                            trace_enabled=trace_enabled,
                            cancellation=cancellation,
                            checkpoint_attempt=checkpoint_attempt,
                        )
                    )
                    running[task] = (node_id, start_attempt)

                if not running:
                    if any(
                        item.status is NodeExecutionStatus.READY for item in state.nodes.values()
                    ):
                        continue
                    break

                done, _ = await asyncio.wait(
                    (*running, cancellation_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_waiter in done:
                    raw_running = {task: node_id for task, (node_id, _) in running.items()}
                    await self._scheduler._cancel_running(raw_running, state)  # noqa: SLF001
                    running.clear()
                    self._scheduler._cancel_unstarted(state)  # noqa: SLF001
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
                    break

                completed = sorted(
                    (task for task in done if task is not cancellation_waiter),
                    key=lambda task: node_order[running[task][0]],
                )
                for task in completed:
                    node_id, _ = running.pop(task)
                    _, result, attempts = await task
                    state.nodes[node_id].attempt = attempts
                    results[node_id] = result
                    should_abort = self._scheduler._apply_node_result(  # noqa: SLF001
                        state,
                        nodes[node_id],
                        result,
                    )
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
                    if should_abort and abort_result is None:
                        abort_result = result

                if abort_result is not None:
                    raw_running = {task: node_id for task, (node_id, _) in running.items()}
                    await self._scheduler._cancel_running(raw_running, state)  # noqa: SLF001
                    running.clear()
                    self._scheduler._cancel_unstarted(state)  # noqa: SLF001
                    await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
                    break
        except asyncio.CancelledError:
            raw_running = {task: node_id for task, (node_id, _) in running.items()}
            await self._scheduler._cancel_running(raw_running, state)  # noqa: SLF001
            running.clear()
            self._scheduler._cancel_unstarted(state)  # noqa: SLF001
            state.status = PlanExecutionStatus.CANCELLED
            state.updated_at = datetime.now(UTC)
            state.completed_at = state.updated_at
            state.state_version += 1
            try:
                await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
            except Exception:
                pass
            raise
        except Exception:
            raw_running = {task: node_id for task, (node_id, _) in running.items()}
            await self._scheduler._cancel_running(raw_running, state)  # noqa: SLF001
            running.clear()
            raise
        finally:
            if not cancellation_waiter.done():
                cancellation_waiter.cancel()
            await asyncio.gather(cancellation_waiter, return_exceptions=True)

        result = self._scheduler._compose_result(  # noqa: SLF001
            plan,
            state,
            results,
            abort_result,
            cancellation,
        )
        self._scheduler._finish_state(state, result)  # noqa: SLF001
        await self._scheduler._checkpoint(checkpoint, state)  # noqa: SLF001
        return SchedulerOutcome(result=result, state=state)

    async def _execute_node(
        self,
        request: Request,
        plan: ExecutionPlan,
        node: PlanNode,
        node_state: NodeExecutionState,
        context: InvocationContext,
        results: dict[str, ResultEnvelope],
        *,
        start_attempt: int,
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationSignal,
        checkpoint_attempt: AttemptCheckpointCallback | None,
    ) -> tuple[str, ResultEnvelope, int]:
        """从持久化 attempt 继续同一次节点执行，保持原 Retry/Deadline 语义。"""

        node_span = (
            self._scheduler._tracer.start_span(  # noqa: SLF001
                f"plan_node.{node.node_id}",
                SpanType.PLAN_NODE,
                parent=parent,
                attributes={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "node_kind": node.kind.value,
                    "resumed": True,
                    "resume_attempt": start_attempt,
                },
            )
            if trace_enabled
            else None
        )
        if node.kind is PlanNodeKind.APPROVAL:
            result = ResultEnvelope.accepted(
                Continuation(
                    plan_id=plan.plan_id,
                    node_id=node.node_id,
                    waiting_reason="approval",
                )
            )
            self._scheduler._lifecycle.finish_from_result(node_span, result)  # noqa: SLF001
            return node.node_id, result, start_attempt

        attempts = start_attempt - 1
        try:
            node_input = self._scheduler._input_resolver.resolve(  # noqa: SLF001
                request,
                node.input_mapping,
                results,
            )
            deadline_at = self._effective_resume_deadline(plan, node, node_state, context)
            execution_context = context.model_copy(
                update={
                    "deadline_at": deadline_at,
                    "cancellation": cancellation.snapshot(),
                    "attributes": {
                        **context.attributes,
                        "plan_id": plan.plan_id,
                        "node_id": node.node_id,
                        **(
                            {"idempotency_key": node.idempotency_key}
                            if node.idempotency_key is not None
                            else {}
                        ),
                    },
                }
            )

            async def on_attempt_started(attempt: ProviderAttempt) -> None:
                nonlocal attempts
                attempts += 1
                if attempts > start_attempt and checkpoint_attempt is not None:
                    await checkpoint_attempt(attempts)
                is_retry = (
                    attempt.retry_attempt > start_attempt
                    if attempt.provider_attempt == 1
                    else attempt.retry_attempt > 1
                )
                if is_retry and node_span is not None:
                    self._scheduler._tracer.add_event(  # noqa: SLF001
                        node_span,
                        "node.retrying",
                        attributes={
                            "attempt": attempt.retry_attempt - 1,
                            "next_attempt": attempt.retry_attempt,
                            "provider_id": attempt.provider_id,
                            "provider_attempt": attempt.provider_attempt,
                            "resumed": True,
                        },
                    )

            result = await self._scheduler._invoker.invoke(  # noqa: SLF001
                node.capability or "",
                node_input,
                execution_context.model_copy(update={"cancellation": cancellation.snapshot()}),
                deadline_at=deadline_at,
                retry_policy=node.retry_policy,
                idempotency_key=node.idempotency_key,
                retry_start_attempt=start_attempt,
                attempt_started=on_attempt_started,
                parent=node_span,
                trace_enabled=trace_enabled,
            )
        except asyncio.CancelledError:
            self._scheduler._lifecycle.finish_cancelled(node_span)  # noqa: SLF001
            raise
        except CheckpointError as exc:
            self._scheduler._lifecycle.finish_error(node_span, exc)  # noqa: SLF001
            raise
        except BindingResolutionError as exc:
            error = RequestError(
                "plan node input binding failed",
                code="HARNESS.PLAN.BINDING_FAILED",
                details={"plan_id": plan.plan_id, "node_id": node.node_id, "reason": str(exc)},
            )
            result = ResultEnvelope.failure(error.to_detail())
        except Exception as exc:
            error = CapabilityError(
                "plan node execution failed",
                code="HARNESS.PLAN.NODE_FAILED",
                details={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "cause_type": type(exc).__name__,
                },
            )
            result = ResultEnvelope.failure(error.to_detail())

        self._scheduler._lifecycle.finish_from_result(node_span, result)  # noqa: SLF001
        return node.node_id, result, attempts

    def _effective_resume_deadline(
        self,
        plan: ExecutionPlan,
        node: PlanNode,
        node_state: NodeExecutionState,
        context: InvocationContext,
    ) -> datetime | None:
        """恢复时不重置已开始节点的 timeout 窗口。"""

        candidates = [item for item in (context.deadline_at, plan.budget.deadline_at) if item]
        if node.timeout_ms is not None:
            started_at = node_state.started_at or self._scheduler._now()  # noqa: SLF001
            candidates.append(started_at + timedelta(milliseconds=node.timeout_ms))
        return min(candidates) if candidates else None

    def _replay_safe(self, node: PlanNode) -> bool:
        if node.kind is PlanNodeKind.APPROVAL:
            return True
        descriptor = self._scheduler._capability_catalog.get(node.capability or "")  # noqa: SLF001
        if descriptor is None:
            return False
        profile = descriptor.execution_profile
        if profile.side_effect in {SideEffectType.NONE, SideEffectType.READ}:
            return True
        if len(self._scheduler._invoker.registry.candidates(node.capability or "")) > 1:  # noqa: SLF001
            return False
        return (
            profile.side_effect is SideEffectType.WRITE
            and profile.idempotency in {IdempotencyType.OPTIONAL, IdempotencyType.REQUIRED}
            and node.idempotency_key is not None
        )

    @staticmethod
    def _restore_abort_result(
        plan: ExecutionPlan,
        state: PlanExecutionState,
    ) -> ResultEnvelope | None:
        for node in plan.nodes:
            node_state = state.nodes[node.node_id]
            if (
                node.failure_policy is FailurePolicy.FAIL_PLAN
                and node_state.status
                in {
                    NodeExecutionStatus.FAILED,
                    NodeExecutionStatus.DENIED,
                    NodeExecutionStatus.CANCELLED,
                }
                and node_state.result is not None
            ):
                return node_state.result
        return None

    @staticmethod
    def _validate_state(plan: ExecutionPlan, state: PlanExecutionState) -> None:
        if state.plan_id != plan.plan_id or state.plan_revision != plan.revision:
            raise RequestError(
                "stored plan state does not match execution plan",
                code="HARNESS.PLAN.STATE_INVALID",
                details={"plan_id": plan.plan_id},
            )
        plan_nodes = {node.node_id: node for node in plan.nodes}
        expected_nodes = set(plan_nodes)
        if set(state.nodes) != expected_nodes:
            raise RequestError(
                "stored plan state has inconsistent nodes",
                code="HARNESS.PLAN.STATE_INVALID",
                details={"plan_id": plan.plan_id},
            )
        for node_id, node_state in state.nodes.items():
            expected = _EXPECTED_RESULT_STATUSES.get(node_state.status)
            if expected is not None:
                if node_state.result is None or node_state.result.status not in expected:
                    raise RequestError(
                        "stored node result is inconsistent with node status",
                        code="HARNESS.PLAN.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node_id},
                    )
                if node_state.status is NodeExecutionStatus.WAITING and (
                    node_state.continuation is None
                    or not node_state.waiting_reason
                    or node_state.result.continuation != node_state.continuation
                ):
                    raise RequestError(
                        "stored waiting node state is incomplete",
                        code="HARNESS.PLAN.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node_id},
                    )
            elif node_state.status is NodeExecutionStatus.RUNNING:
                if (
                    node_state.attempt < 1
                    or node_state.attempt > plan_nodes[node_id].retry_policy.max_attempts
                    or node_state.started_at is None
                    or node_state.result is not None
                ):
                    raise RequestError(
                        "stored running node state is invalid",
                        code="HARNESS.PLAN.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node_id},
                    )
            elif node_state.status is NodeExecutionStatus.READY and node_state.attempt > 0:
                if (
                    node_state.attempt > plan_nodes[node_id].retry_policy.max_attempts
                    or node_state.started_at is None
                    or node_state.result is not None
                ):
                    raise RequestError(
                        "stored replay-ready node state is invalid",
                        code="HARNESS.PLAN.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node_id},
                    )
            elif (
                node_state.status
                in {
                    NodeExecutionStatus.PENDING,
                    NodeExecutionStatus.READY,
                    NodeExecutionStatus.SKIPPED,
                }
                and node_state.result is not None
            ):
                raise RequestError(
                    "stored unexecuted node unexpectedly contains a result",
                    code="HARNESS.PLAN.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node_id},
                )

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
