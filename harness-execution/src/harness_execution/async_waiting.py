"""Capability Async WAITING 的持久化等待与 completion 协调。"""

from __future__ import annotations

from datetime import UTC, datetime

from harness_contracts import (
    Continuation,
    ExecutionPlan,
    NodeExecutionStatus,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanNodeKind,
    RequestError,
    ResultEnvelope,
    ResultStatus,
)

from .scheduler import BasicScheduler

_TERMINAL_RESULT_STATUSES = {
    ResultStatus.SUCCESS,
    ResultStatus.PARTIAL,
    ResultStatus.FAILED,
    ResultStatus.DENIED,
    ResultStatus.CANCELLED,
}


class AsyncWaitingCoordinator:
    """管理 Capability ``ACCEPTED`` 产生的异步 WAITING 与外部 completion。

    Provider 只负责返回 ``ACCEPTED + Continuation.job_ref``；Scheduler 把节点转换成
    WAITING。本协调器把 Continuation 归一化为稳定 ``plan_id + node_id + job_ref``，
    写入 ``pending_jobs``，并把外部 terminal ResultEnvelope 应用回标准节点状态。
    """

    def __init__(self, scheduler: BasicScheduler) -> None:
        if not isinstance(scheduler, BasicScheduler):
            raise TypeError("scheduler must be BasicScheduler")
        self._scheduler = scheduler

    def ensure_waiting_jobs(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
    ) -> tuple[Continuation, ...]:
        """为异步 WAITING Capability 补全并持久化稳定 pending job 引用。

        Scheduler 会先 checkpoint WAITING，再回到 ExecutionEngine。若进程恰好在
        WAITING checkpoint 后、``pending_jobs`` 落盘前退出，Resume 会再次调用本
        方法，因此该崩溃窗口可以像 ApprovalRequest 一样自愈。
        """

        self.validate(plan, state, allow_unmaterialized=True)
        existing_by_node = {
            item.node_id: item for item in state.pending_jobs if item.node_id is not None
        }
        materialized: list[Continuation] = []
        materialized_nodes: set[str] = set()
        changed = False

        for node in plan.nodes:
            if node.kind is not PlanNodeKind.CAPABILITY:
                continue
            node_state = state.nodes[node.node_id]
            if node_state.status is not NodeExecutionStatus.WAITING:
                continue
            # Policy-triggered Approval 也会让 CAPABILITY 节点进入 WAITING，但它
            # 由 ApprovalCoordinator 管理，不属于异步 Job completion。
            if node_state.waiting_reason == "policy_approval":
                continue
            continuation = node_state.continuation
            if continuation is None:
                raise RequestError(
                    "waiting async capability has no continuation",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.approval_id is not None:
                raise RequestError(
                    "async capability continuation cannot contain approval_id",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.job_ref is None:
                raise RequestError(
                    "async capability continuation requires job_ref",
                    code="HARNESS.ASYNC.JOB_REF_REQUIRED",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.plan_id not in {None, plan.plan_id}:
                raise RequestError(
                    "async continuation references another plan",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.node_id not in {None, node.node_id}:
                raise RequestError(
                    "async continuation references another node",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )

            normalized = continuation.model_copy(
                update={"plan_id": plan.plan_id, "node_id": node.node_id}
            )
            node_changed = normalized != continuation
            if node_changed:
                node_state.continuation = normalized
                if (
                    node_state.result is None
                    or node_state.result.status is not ResultStatus.ACCEPTED
                ):
                    raise RequestError(
                        "waiting async capability has no accepted result",
                        code="HARNESS.ASYNC.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node.node_id},
                    )
                node_state.result = ResultEnvelope.accepted(
                    normalized,
                    trace_id=node_state.result.trace_id,
                    metadata=dict(node_state.result.metadata),
                )
                continuation = normalized
                changed = True

            pending = existing_by_node.get(node.node_id)
            if pending is None:
                state.pending_jobs.append(normalized)
                existing_by_node[node.node_id] = normalized
                materialized.append(normalized)
                materialized_nodes.add(node.node_id)
                changed = True
            elif pending != normalized:
                raise RequestError(
                    "pending async job is inconsistent with node continuation",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            elif node_changed and node.node_id not in materialized_nodes:
                # Continuation 可能只缺 plan_id/node_id，而 pending_jobs 已存在。调用方
                # 仍需知道 state 被修复过，从而在返回 ACCEPTED 前执行一次 checkpoint。
                materialized.append(normalized)
                materialized_nodes.add(node.node_id)

        if changed:
            self._touch(state)
        self.validate(plan, state, allow_unmaterialized=False)
        return tuple(materialized)

    def resolve(
        self,
        record: PlanExecutionRecord,
        node_id: str,
        terminal_result: ResultEnvelope,
    ) -> PlanExecutionRecord:
        """把外部异步 terminal result 应用到一个 pending job，但不自行 Resume。"""

        if not isinstance(record, PlanExecutionRecord):
            raise TypeError("record must be PlanExecutionRecord")
        if not isinstance(node_id, str) or not node_id.strip():
            raise TypeError("node_id must be a non-empty string")
        if not isinstance(terminal_result, ResultEnvelope):
            raise TypeError("terminal_result must be ResultEnvelope")
        if terminal_result.status not in _TERMINAL_RESULT_STATUSES:
            raise RequestError(
                "async completion result must be terminal",
                code="HARNESS.ASYNC.RESULT_NOT_TERMINAL",
                details={
                    "plan_id": record.plan_id,
                    "node_id": node_id,
                    "status": terminal_result.status.value,
                },
            )

        plan = record.plan
        state = self._snapshot(record.state)
        self.ensure_waiting_jobs(plan, state)
        pending = next((item for item in state.pending_jobs if item.node_id == node_id), None)
        if pending is None:
            raise RequestError(
                "async node is not pending completion",
                code="HARNESS.ASYNC.NOT_PENDING",
                details={"plan_id": plan.plan_id, "node_id": node_id},
            )

        node = next((item for item in plan.nodes if item.node_id == node_id), None)
        node_state = state.nodes.get(node_id)
        if (
            node is None
            or node.kind is not PlanNodeKind.CAPABILITY
            or node_state is None
            or node_state.status is not NodeExecutionStatus.WAITING
            or node_state.continuation != pending
            or pending.job_ref is None
        ):
            raise RequestError(
                "pending async job is inconsistent with node state",
                code="HARNESS.ASYNC.STATE_INVALID",
                details={"plan_id": plan.plan_id, "node_id": node_id},
            )

        state.pending_jobs = [item for item in state.pending_jobs if item.node_id != node_id]
        node_state.waiting_reason = None
        node_state.continuation = None
        self._scheduler._apply_node_result(state, node, terminal_result)  # noqa: SLF001
        self._append_completion_audit(state, node_id, pending, terminal_result)
        return PlanExecutionRecord(
            plan_id=record.plan_id,
            plan=plan,
            context=record.context,
            state=state,
        )

    def validate(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
        *,
        allow_unmaterialized: bool = False,
    ) -> None:
        """验证 pending_jobs 与 Capability WAITING 状态的一致性。"""

        node_index = {node.node_id: node for node in plan.nodes}
        pending_nodes: set[str] = set()
        for continuation in state.pending_jobs:
            node_id = continuation.node_id
            if node_id is None or node_id in pending_nodes:
                raise RequestError(
                    "stored pending jobs contain duplicate or unbound nodes",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id},
                )
            pending_nodes.add(node_id)
            node = node_index.get(node_id)
            node_state = state.nodes.get(node_id)
            if (
                continuation.plan_id != plan.plan_id
                or continuation.job_ref is None
                or continuation.approval_id is not None
                or node is None
                or node.kind is not PlanNodeKind.CAPABILITY
                or node_state is None
                or node_state.status is not NodeExecutionStatus.WAITING
                or node_state.continuation != continuation
                or node_state.result is None
                or node_state.result.status is not ResultStatus.ACCEPTED
                or node_state.result.continuation != continuation
            ):
                raise RequestError(
                    "stored pending async job is inconsistent with node state",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node_id},
                )

        if allow_unmaterialized:
            return
        for node in plan.nodes:
            if node.kind is not PlanNodeKind.CAPABILITY:
                continue
            node_state = state.nodes[node.node_id]
            if node_state.status is not NodeExecutionStatus.WAITING:
                continue
            if node_state.waiting_reason == "policy_approval":
                continue
            continuation = node_state.continuation
            if continuation is None:
                raise RequestError(
                    "waiting capability has no continuation",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.approval_id is not None:
                raise RequestError(
                    "async capability continuation cannot contain approval_id",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.job_ref is None:
                raise RequestError(
                    "async capability continuation requires job_ref",
                    code="HARNESS.ASYNC.JOB_REF_REQUIRED",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if node.node_id not in pending_nodes:
                raise RequestError(
                    "waiting async capability has no pending job",
                    code="HARNESS.ASYNC.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )

    @staticmethod
    def refresh_accepted_result(
        result: ResultEnvelope,
        state: PlanExecutionState,
    ) -> ResultEnvelope:
        """让当前 API 的 ACCEPTED 返回归一化后的 plan/node/job continuation。"""

        if result.status is not ResultStatus.ACCEPTED or result.continuation is None:
            return result
        node_id = result.continuation.node_id
        if node_id is None:
            # Provider 原始 continuation 可能没有 node_id；按 job_ref 找对应 WAITING。
            candidates = [
                item for item in state.pending_jobs if item.job_ref == result.continuation.job_ref
            ]
            if not candidates:
                return result
            return ResultEnvelope.accepted(
                candidates[0],
                trace_id=result.trace_id,
                metadata=dict(result.metadata),
            )
        node_state = state.nodes.get(node_id)
        if (
            node_state is None
            or node_state.status is not NodeExecutionStatus.WAITING
            or node_state.continuation is None
        ):
            return result
        return ResultEnvelope.accepted(
            node_state.continuation,
            trace_id=result.trace_id,
            metadata=dict(result.metadata),
        )

    @staticmethod
    def _append_completion_audit(
        state: PlanExecutionState,
        node_id: str,
        continuation: Continuation,
        terminal_result: ResultEnvelope,
    ) -> None:
        history = state.metadata.setdefault("async_completions", [])
        if not isinstance(history, list):
            raise RequestError(
                "stored async completion audit is invalid",
                code="HARNESS.ASYNC.STATE_INVALID",
                details={"plan_id": state.plan_id},
            )
        history.append(
            {
                "node_id": node_id,
                "job_ref": continuation.job_ref,
                "status": terminal_result.status.value,
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )

    def _touch(self, state: PlanExecutionState) -> None:
        self._scheduler._touch(state)  # noqa: SLF001

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
