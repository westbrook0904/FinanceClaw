"""Explicit / Policy-triggered Human Approval 的持久化协调。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from harness_contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalGrant,
    ApprovalRequest,
    EgressType,
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
    SideEffectType,
)

from .scheduler import BasicScheduler


_EXPLICIT_WAITING = "approval"
_POLICY_WAITING = "policy_approval"


class ApprovalCoordinator:
    """把两种 Approval 来源统一到 ApprovalRequest/Decision/WAITING/Resume。"""

    def __init__(self, scheduler: BasicScheduler) -> None:
        if not isinstance(scheduler, BasicScheduler):
            raise TypeError("scheduler must be BasicScheduler")
        self._scheduler = scheduler

    def ensure_waiting_requests(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
    ) -> tuple[ApprovalRequest, ...]:
        """为 WAITING Approval 补全稳定 request/approval_id，并支持 crash-window 自愈。"""

        self.validate(plan, state, allow_unmaterialized=True)
        existing_by_node = {item.node_id: item for item in state.pending_approvals}
        materialized: list[ApprovalRequest] = []
        materialized_ids: set[str] = set()
        changed = False

        for node in plan.nodes:
            node_state = state.nodes[node.node_id]
            if node_state.status is not NodeExecutionStatus.WAITING:
                continue
            if not self._is_approval_waiting(node, node_state.waiting_reason):
                continue

            approval = existing_by_node.get(node.node_id)
            if approval is None:
                approval = (
                    self._build_explicit_request(plan, node)
                    if node.kind is PlanNodeKind.APPROVAL
                    else self._build_policy_request(plan, node, node_state.result)
                )
                state.pending_approvals.append(approval)
                existing_by_node[node.node_id] = approval
                materialized.append(approval)
                materialized_ids.add(approval.approval_id)
                changed = True

            continuation = node_state.continuation
            if continuation is None:
                raise self._state_error(
                    plan, node.node_id, "waiting approval has no continuation"
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
                    raise self._state_error(
                        plan,
                        node.node_id,
                        "waiting approval has no accepted result",
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
        """应用一个 pending 决策；批准 Policy Approval 时生成 Grant 后重新执行节点。"""

        if not isinstance(record, PlanExecutionRecord):
            raise TypeError("record must be PlanExecutionRecord")
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("decision must be ApprovalDecision")

        plan = record.plan
        state = self._snapshot(record.state)
        self.ensure_waiting_requests(plan, state)
        request = next(
            (item for item in state.pending_approvals if item.approval_id == decision.approval_id),
            None,
        )
        if request is None:
            raise RequestError(
                "approval request is not pending",
                code="HARNESS.APPROVAL.NOT_PENDING",
                details={"plan_id": plan.plan_id, "approval_id": decision.approval_id},
            )
        if request.plan_id != plan.plan_id:
            raise RequestError(
                "approval request belongs to another plan",
                code="HARNESS.APPROVAL.PLAN_MISMATCH",
                details={"plan_id": plan.plan_id, "approval_id": decision.approval_id},
            )

        node = next((item for item in plan.nodes if item.node_id == request.node_id), None)
        node_state = state.nodes.get(request.node_id)
        if (
            node is None
            or node_state is None
            or node_state.status is not NodeExecutionStatus.WAITING
            or node_state.continuation is None
            or node_state.continuation.approval_id != decision.approval_id
            or not self._is_approval_waiting(node, node_state.waiting_reason)
        ):
            raise self._state_error(
                plan,
                request.node_id,
                "approval node is not waiting for this decision",
                approval_id=decision.approval_id,
            )

        state.pending_approvals = [
            item for item in state.pending_approvals if item.approval_id != decision.approval_id
        ]
        self._append_decision_audit(state, request, decision)

        if (
            node.kind is PlanNodeKind.CAPABILITY
            and decision.decision is ApprovalDecisionType.APPROVED
        ):
            self._append_grant(state, request, decision)
            # Policy 尚未真正调用 Provider，因此审批通过后回到“未执行 READY”，而不是
            # 把原 WAITING attempt 当作 crash 中断的 Provider attempt 去做 replay guard。
            node_state.status = NodeExecutionStatus.READY
            node_state.attempt = 0
            node_state.started_at = None
            node_state.completed_at = None
            node_state.result = None
            node_state.error = None
            node_state.waiting_reason = None
            node_state.continuation = None
            self._touch(state)
        else:
            node_state.waiting_reason = None
            node_state.continuation = None
            result = self._decision_result(plan, node, decision)
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
        """验证 pending approval 与显式/Policy WAITING 节点的一致性。"""

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
                or node_state is None
                or node_state.status is not NodeExecutionStatus.WAITING
                or node_state.continuation is None
                or not self._is_approval_waiting(node, node_state.waiting_reason)
                or (
                    node_state.continuation.approval_id is not None
                    and node_state.continuation.approval_id != request.approval_id
                )
            ):
                raise self._state_error(
                    plan,
                    request.node_id,
                    "stored approval request is inconsistent with node state",
                    approval_id=request.approval_id,
                )
            if node.kind is PlanNodeKind.APPROVAL and request.capability is not None:
                raise self._state_error(plan, request.node_id, "explicit approval has capability")
            if node.kind is PlanNodeKind.CAPABILITY and request.capability != node.capability:
                raise self._state_error(
                    plan,
                    request.node_id,
                    "policy approval capability does not match plan node",
                )

        if allow_unmaterialized:
            return
        for node in plan.nodes:
            node_state = state.nodes[node.node_id]
            if (
                node_state.status is NodeExecutionStatus.WAITING
                and self._is_approval_waiting(node, node_state.waiting_reason)
                and node.node_id not in approval_nodes
            ):
                raise self._state_error(
                    plan,
                    node.node_id,
                    "waiting approval has no pending approval request",
                )

    @staticmethod
    def refresh_accepted_result(
        result: ResultEnvelope,
        state: PlanExecutionState,
    ) -> ResultEnvelope:
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
            or node_state.continuation.approval_id is None
        ):
            return result
        return ResultEnvelope.accepted(
            node_state.continuation,
            trace_id=result.trace_id,
            metadata=dict(result.metadata),
        )

    @staticmethod
    def grants(state: PlanExecutionState) -> tuple[ApprovalGrant, ...]:
        payload = state.metadata.get("approval_grants", [])
        if not isinstance(payload, tuple | list):
            raise RequestError(
                "stored approval grants are invalid",
                code="HARNESS.APPROVAL.STATE_INVALID",
                details={"plan_id": state.plan_id},
            )
        grants: list[ApprovalGrant] = []
        for raw in payload:
            try:
                grants.append(ApprovalGrant.model_validate(raw))
            except Exception as exc:
                raise RequestError(
                    "stored approval grant is invalid",
                    code="HARNESS.APPROVAL.STATE_INVALID",
                    details={"plan_id": state.plan_id},
                ) from exc
        return tuple(grants)

    @staticmethod
    def _is_approval_waiting(node: PlanNode, waiting_reason: str | None) -> bool:
        return (
            node.kind is PlanNodeKind.APPROVAL and waiting_reason == _EXPLICIT_WAITING
        ) or (
            node.kind is PlanNodeKind.CAPABILITY and waiting_reason == _POLICY_WAITING
        )

    @staticmethod
    def _build_explicit_request(plan: ExecutionPlan, node: PlanNode) -> ApprovalRequest:
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
            parameter_summary={"parameter_names": parameter_names} if parameter_names else {},
            reason=reason,
            metadata={"source": "explicit_node"},
        )

    @staticmethod
    def _build_policy_request(
        plan: ExecutionPlan,
        node: PlanNode,
        accepted_result: ResultEnvelope | None,
    ) -> ApprovalRequest:
        if accepted_result is None or accepted_result.status is not ResultStatus.ACCEPTED:
            raise ApprovalCoordinator._state_error(
                plan,
                node.node_id,
                "policy approval has no accepted result",
            )
        payload = accepted_result.metadata.get("approval_request")
        if not isinstance(payload, Mapping):
            raise ApprovalCoordinator._state_error(
                plan,
                node.node_id,
                "policy approval request summary is missing",
            )
        capability = payload.get("capability")
        reason = payload.get("reason")
        side_effect = payload.get("side_effect", SideEffectType.NONE.value)
        egress = payload.get("egress", EgressType.NONE.value)
        policy = payload.get("policy")
        parameter_summary = payload.get("parameter_summary", {})
        if capability != node.capability or not isinstance(reason, str) or not reason.strip():
            raise ApprovalCoordinator._state_error(
                plan,
                node.node_id,
                "policy approval request summary is invalid",
            )
        if not isinstance(parameter_summary, Mapping):
            parameter_summary = {}
        try:
            side_effect_value = SideEffectType(side_effect)
            egress_value = EgressType(egress)
        except ValueError as exc:
            raise ApprovalCoordinator._state_error(
                plan,
                node.node_id,
                "policy approval execution profile is invalid",
            ) from exc
        metadata = {"source": "policy"}
        if isinstance(policy, str) and policy.strip():
            metadata["policy"] = policy.strip()
        return ApprovalRequest(
            approval_id=uuid4().hex,
            plan_id=plan.plan_id,
            node_id=node.node_id,
            capability=node.capability,
            side_effect=side_effect_value,
            egress=egress_value,
            parameter_summary=dict(parameter_summary),
            reason=reason.strip(),
            metadata=metadata,
        )

    @staticmethod
    def _decision_result(
        plan: ExecutionPlan,
        node: PlanNode,
        decision: ApprovalDecision,
    ) -> ResultEnvelope:
        if decision.decision is ApprovalDecisionType.APPROVED:
            return ResultEnvelope.success(
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
        return ResultEnvelope.denied(
            error.to_detail(),
            metadata={
                "plan_id": plan.plan_id,
                "node_id": node.node_id,
                "approval_id": decision.approval_id,
            },
        )

    @staticmethod
    def _append_grant(
        state: PlanExecutionState,
        request: ApprovalRequest,
        decision: ApprovalDecision,
    ) -> None:
        grant = ApprovalGrant(
            approval_id=decision.approval_id,
            plan_id=request.plan_id,
            node_id=request.node_id,
            decided_by=decision.decided_by,
            granted_at=decision.decided_at,
            reason=decision.reason,
            metadata={
                "source": "policy",
                **(
                    {"policy": request.metadata["policy"]}
                    if isinstance(request.metadata.get("policy"), str)
                    else {}
                ),
            },
        )
        current = state.metadata.get("approval_grants", [])
        history = list(current) if isinstance(current, tuple | list) else []
        history = [
            item
            for item in history
            if not (
                isinstance(item, Mapping)
                and item.get("plan_id") == grant.plan_id
                and item.get("node_id") == grant.node_id
            )
        ]
        history.append(grant.model_dump(mode="json"))
        state.metadata["approval_grants"] = history

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
                "source": request.metadata.get("source", "unknown"),
                "decision": decision.decision.value,
                "decided_by": decision.decided_by,
                "decided_at": decision.decided_at.isoformat(),
                **({"reason": decision.reason} if decision.reason else {}),
            }
        )
        state.metadata["approval_decisions"] = history

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
    def _state_error(
        plan: ExecutionPlan,
        node_id: str,
        message: str,
        *,
        approval_id: str | None = None,
    ) -> RequestError:
        return RequestError(
            message,
            code="HARNESS.APPROVAL.STATE_INVALID",
            details={
                "plan_id": plan.plan_id,
                "node_id": node_id,
                **({"approval_id": approval_id} if approval_id else {}),
            },
        )

    @staticmethod
    def _touch(state: PlanExecutionState) -> None:
        state.updated_at = datetime.now(UTC)
        state.state_version += 1

    @staticmethod
    def _snapshot(state: PlanExecutionState) -> PlanExecutionState:
        return PlanExecutionState.model_validate(state.model_dump(mode="json"))
