"""显式 Human Approval 的持久化等待与决策协调。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ExecutionPlan,
    NodeExecutionStatus,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanNode,
    PlanNodeKind,
    PolicyError,
    RequestError,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
)

from .scheduler import BasicScheduler


class ApprovalCoordinator:
    """管理显式 ``APPROVAL`` 节点的等待快照和外部审批决定。

    Scheduler 仍负责 DAG READY/RUNNING/WAITING/terminal 状态机；本协调器只补全
    Human Approval 特有的持久化数据，并把外部 ``ApprovalDecision`` 转换成节点
    的标准 ResultEnvelope。StateStore 保持纯快照存储，不理解审批业务语义。
    """

    def __init__(self, scheduler: BasicScheduler) -> None:
        if not isinstance(scheduler, BasicScheduler):
            raise TypeError("scheduler must be BasicScheduler")
        self._scheduler = scheduler

    def ensure_waiting_requests(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
    ) -> tuple[ApprovalRequest, ...]:
        """为已经进入 WAITING 的显式 Approval Node 补全稳定 ApprovalRequest。

        正常路径会在当前 API 返回前调用本方法并 checkpoint。若进程恰好在
        Scheduler 的 WAITING checkpoint 后、ApprovalRequest 落盘前退出，Resume
        也会再次调用本方法，因此这个小窗口仍然可以自愈。
        """

        self.validate(plan, state, allow_unmaterialized=True)
        existing_by_node = {item.node_id: item for item in state.pending_approvals}
        materialized: list[ApprovalRequest] = []
        materialized_ids: set[str] = set()
        changed = False

        for node in plan.nodes:
            if node.kind is not PlanNodeKind.APPROVAL:
                continue
            node_state = state.nodes[node.node_id]
            if (
                node_state.status is not NodeExecutionStatus.WAITING
                or node_state.waiting_reason != "approval"
            ):
                continue

            approval = existing_by_node.get(node.node_id)
            if approval is None:
                approval = self._build_request(plan, node)
                state.pending_approvals.append(approval)
                existing_by_node[node.node_id] = approval
                materialized.append(approval)
                materialized_ids.add(approval.approval_id)
                changed = True

            continuation = node_state.continuation
            if continuation is None:
                raise RequestError(
                    "waiting approval node has no continuation",
                    code="HARNESS.APPROVAL.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )
            if continuation.approval_id != approval.approval_id:
                continuation = continuation.model_copy(
                    update={"approval_id": approval.approval_id}
                )
                node_state.continuation = continuation
                if (
                    node_state.result is None
                    or node_state.result.status is not ResultStatus.ACCEPTED
                ):
                    raise RequestError(
                        "waiting approval node has no accepted result",
                        code="HARNESS.APPROVAL.STATE_INVALID",
                        details={"plan_id": plan.plan_id, "node_id": node.node_id},
                    )
                node_state.result = ResultEnvelope.accepted(
                    continuation,
                    trace_id=node_state.result.trace_id,
                    metadata=dict(node_state.result.metadata),
                )
                if approval.approval_id not in materialized_ids:
                    materialized.append(approval)
                    materialized_ids.add(approval.approval_id)
                changed = True

        if changed:
            self._touch(state)
        self.validate(plan, state, allow_unmaterialized=False)
        return tuple(materialized)

    def resolve(
        self,
        record: PlanExecutionRecord,
        decision: ApprovalDecision,
    ) -> PlanExecutionRecord:
        """把一个 pending ApprovalDecision 应用到快照，但不自行执行 Resume。"""

        if not isinstance(record, PlanExecutionRecord):
            raise TypeError("record must be PlanExecutionRecord")
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("decision must be ApprovalDecision")

        plan = record.plan
        state = self._snapshot(record.state)
        self.ensure_waiting_requests(plan, state)

        request = next(
            (
                item
                for item in state.pending_approvals
                if item.approval_id == decision.approval_id
            ),
            None,
        )
        if request is None:
            raise RequestError(
                "approval request is not pending",
                code="HARNESS.APPROVAL.NOT_PENDING",
                details={
                    "plan_id": plan.plan_id,
                    "approval_id": decision.approval_id,
                },
            )
        if request.plan_id != plan.plan_id:
            raise RequestError(
                "approval request belongs to another plan",
                code="HARNESS.APPROVAL.PLAN_MISMATCH",
                details={
                    "plan_id": plan.plan_id,
                    "approval_id": decision.approval_id,
                },
            )

        node = next((item for item in plan.nodes if item.node_id == request.node_id), None)
        if node is None or node.kind is not PlanNodeKind.APPROVAL:
            raise RequestError(
                "approval request does not reference an approval node",
                code="HARNESS.APPROVAL.STATE_INVALID",
                details={
                    "plan_id": plan.plan_id,
                    "approval_id": decision.approval_id,
                    "node_id": request.node_id,
                },
            )
        node_state = state.nodes[node.node_id]
        if (
            node_state.status is not NodeExecutionStatus.WAITING
            or node_state.continuation is None
            or node_state.continuation.approval_id != decision.approval_id
        ):
            raise RequestError(
                "approval node is not waiting for this decision",
                code="HARNESS.APPROVAL.STATE_INVALID",
                details={
                    "plan_id": plan.plan_id,
                    "approval_id": decision.approval_id,
                    "node_id": node.node_id,
                },
            )

        state.pending_approvals = [
            item for item in state.pending_approvals if item.approval_id != decision.approval_id
        ]
        node_state.waiting_reason = None
        node_state.continuation = None

        if decision.decision is ApprovalDecisionType.APPROVED:
            result = ResultEnvelope.success(
                ResultOutput(
                    type="approval",
                    data={
                        "approval_id": decision.approval_id,
                        "decision": decision.decision.value,
                        "decided_by": decision.decided_by,
                        "decided_at": decision.decided_at.isoformat(),
                        **({"reason": decision.reason} if decision.reason else {}),
                    },
                ),
                metadata={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "approval_id": decision.approval_id,
                },
            )
        else:
            error = PolicyError(
                "approval was rejected",
                code="HARNESS.APPROVAL.REJECTED",
                details={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "approval_id": decision.approval_id,
                    "decided_by": decision.decided_by,
                    **({"reason": decision.reason} if decision.reason else {}),
                },
            )
            result = ResultEnvelope.denied(
                error.to_detail(),
                metadata={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "approval_id": decision.approval_id,
                },
            )

        self._append_decision_audit(state, request, decision)
        self._scheduler._apply_node_result(state, node, result)  # noqa: SLF001
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
        """验证 pending approval 与节点 WAITING 状态的一致性。"""

        node_index = {node.node_id: node for node in plan.nodes}
        approval_ids: set[str] = set()
        approval_nodes: set[str] = set()
        for request in state.pending_approvals:
            if request.approval_id in approval_ids or request.node_id in approval_nodes:
                raise RequestError(
                    "stored approval requests contain duplicates",
                    code="HARNESS.APPROVAL.STATE_INVALID",
                    details={"plan_id": plan.plan_id},
                )
            approval_ids.add(request.approval_id)
            approval_nodes.add(request.node_id)
            node = node_index.get(request.node_id)
            node_state = state.nodes.get(request.node_id)
            if (
                request.plan_id != plan.plan_id
                or node is None
                or node.kind is not PlanNodeKind.APPROVAL
                or node_state is None
                or node_state.status is not NodeExecutionStatus.WAITING
                or node_state.waiting_reason != "approval"
                or node_state.continuation is None
                or (
                    node_state.continuation.approval_id is not None
                    and node_state.continuation.approval_id != request.approval_id
                )
            ):
                raise RequestError(
                    "stored approval request is inconsistent with node state",
                    code="HARNESS.APPROVAL.STATE_INVALID",
                    details={
                        "plan_id": plan.plan_id,
                        "approval_id": request.approval_id,
                        "node_id": request.node_id,
                    },
                )

        if allow_unmaterialized:
            return
        for node in plan.nodes:
            if node.kind is not PlanNodeKind.APPROVAL:
                continue
            node_state = state.nodes[node.node_id]
            if (
                node_state.status is NodeExecutionStatus.WAITING
                and node_state.waiting_reason == "approval"
                and node.node_id not in approval_nodes
            ):
                raise RequestError(
                    "waiting approval node has no pending approval request",
                    code="HARNESS.APPROVAL.STATE_INVALID",
                    details={"plan_id": plan.plan_id, "node_id": node.node_id},
                )

    @staticmethod
    def refresh_accepted_result(
        result: ResultEnvelope,
        state: PlanExecutionState,
    ) -> ResultEnvelope:
        """让当前 API 的 ACCEPTED 结果返回已持久化的 approval_id。"""

        if result.status is not ResultStatus.ACCEPTED or result.continuation is None:
            return result
        node_id = result.continuation.node_id
        if node_id is None:
            return result
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
    def _build_request(plan: ExecutionPlan, node: PlanNode) -> ApprovalRequest:
        reason = ApprovalCoordinator._safe_metadata_string(
            node,
            "approval_reason",
        ) or "explicit approval required"
        resource_category = ApprovalCoordinator._safe_metadata_string(
            node,
            "approval_resource_category",
        )
        parameter_names = ApprovalCoordinator._safe_parameter_names(node)
        return ApprovalRequest(
            approval_id=uuid4().hex,
            plan_id=plan.plan_id,
            node_id=node.node_id,
            resource_category=resource_category,
            parameter_summary=(
                {"parameter_names": parameter_names} if parameter_names else {}
            ),
            reason=reason,
            metadata={"source": "explicit_node"},
        )

    @staticmethod
    def _safe_metadata_string(node: PlanNode, key: str) -> str | None:
        value = node.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _safe_parameter_names(node: PlanNode) -> list[str]:
        value = node.metadata.get("approval_parameter_names")
        if not isinstance(value, tuple | list):
            return []
        names: list[str] = []
        for item in value[:32]:
            if isinstance(item, str) and item.strip():
                names.append(item.strip()[:128])
        return names

    @staticmethod
    def _append_decision_audit(
        state: PlanExecutionState,
        request: ApprovalRequest,
        decision: ApprovalDecision,
    ) -> None:
        current = state.metadata.get("approval_decisions", [])
        history = list(current) if isinstance(current, tuple | list) else []
        history.append(
            {
                "approval_id": decision.approval_id,
                "node_id": request.node_id,
                "decision": decision.decision.value,
                "decided_by": decision.decided_by,
                "decided_at": decision.decided_at.isoformat(),
                **({"reason": decision.reason} if decision.reason else {}),
            }
        )
        state.metadata["approval_decisions"] = history

    @staticmethod
    def _touch(state: PlanExecutionState) -> None:
        state.updated_at = datetime.now(UTC)
        state.state_version += 1

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
