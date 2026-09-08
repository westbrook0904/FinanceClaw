"""旧根分页盘点、只读 shadow 与唯一驱动接管；缺证据不启动、不猜测。"""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select, union, update

from financeclaw.coordination.application.releases import release_ref
from financeclaw.coordination.application.transitions import (
    CoordinationTransitions,
    interaction_projection,
)
from financeclaw.coordination.repository import DRIVER_VERSION, aware, now
from financeclaw.kernel.coordination import DelegationRequest, InteractionRequest, handoff_input
from financeclaw.shared.conversation.tables import (
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
)
from financeclaw.shared.execution_ledger.coordination_tables import (
    BackendAttemptRow,
    CoordinatedRunRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.cutover_tables import (
    CoordinationControlRow,
    LegacyAdoptionRow,
)
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.execution_ledger.workflow_tables import WorkflowApprovalRow, WorkflowRunRow

TERMINAL = {"completed", "failed", "cancelled", "rejected", "expired"}
BLOCKERS = {
    "legacy_resume_requires_application_evidence",
    "legacy_command_snapshot_mismatch",
    "legacy_operation_history_requires_application_evidence",
    "legacy_execution_identity_mismatch",
    "legacy_command_owner_mismatch",
    "legacy_submission_uncertain",
    "prepared_operation_has_remote_attempt",
    "legacy_attempt_binding_mismatch",
    "legacy_active_position_mismatch",
    "legacy_backend_not_quiescent",
    "legacy_delegation_position_mismatch",
}


def reason(exc):
    """只允许固定诊断码；HTTP、数据库和用户载荷不进入盘点结果。"""
    return (
        str(exc)
        if isinstance(exc, ExecutionConflict) and str(exc) in BLOCKERS
        else "legacy_evidence_unverifiable"
    )


def matches_arguments(request, record):
    """比较原传入字段与已冻结的默认值，兼容旧 V1 Agent 的空 arguments 字段。"""
    original, saved = handoff_input(request.handoff), record["arguments"]
    if record["kind"] == "agent":
        return {**original, "arguments": original.get("arguments", {})} == saved
    return all(key in saved and saved[key] == value for key, value in original.items())


def export(row):
    """原始行完整归档；日期统一 UTC，避免 SQLite 与 PostgreSQL 摘要差异。"""
    if row is None:
        return None
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = aware(value).isoformat() if isinstance(value, datetime) else value
    return result


class LegacyMigration:
    """只接管可证明的静止执行树；已接管根永不切回 legacy，历史终态只读。"""

    def __init__(self, store, releases, inspector):
        """注入只读旧协议解释器，核心只接收标准命令与观察。"""
        self.store, self.releases, self.inspector = store, releases, inspector

    def _facts(self, session, root_id):
        """完整原始证据进入私有归档；CLI 只显示其摘要与阻塞原因。"""
        turn = session.scalar(
            select(ConversationTurnRow).where(ConversationTurnRow.run_id == root_id)
        )
        executions = list(
            session.scalars(
                select(RunExecutionRow)
                .where(RunExecutionRow.root_run_id == root_id)
                .order_by(RunExecutionRow.run_id)
                .limit(501)
            )
        )
        ids = [row.run_id for row in executions]
        return {
            "turn": export(turn),
            "conversation": export(session.get(ConversationRow, turn.conversation_id))
            if turn
            else None,
            "executions": [export(row) for row in executions],
            "operations": [
                export(row)
                for row in session.scalars(
                    select(RunOperationRow)
                    .where(RunOperationRow.run_id.in_(ids))
                    .order_by(RunOperationRow.operation_id)
                    .limit(501)
                )
            ],
            "delegations": [
                export(row)
                for row in session.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.parent_run_id.in_(ids))
                    .order_by(DelegationRow.delegation_id)
                    .limit(501)
                )
            ],
            "interactions": [
                export(row)
                for row in session.scalars(
                    select(PendingInteractionRow)
                    .where(PendingInteractionRow.root_run_id == root_id)
                    .order_by(PendingInteractionRow.interaction_id)
                    .limit(501)
                )
            ],
            "workflows": [
                export(row)
                for row in session.scalars(
                    select(WorkflowRunRow)
                    .where(WorkflowRunRow.run_id.in_([root_id, *ids]))
                    .order_by(WorkflowRunRow.run_id)
                    .limit(501)
                )
            ],
            "approvals": [
                export(row)
                for row in session.scalars(
                    select(WorkflowApprovalRow)
                    .where(WorkflowApprovalRow.run_id.in_(ids))
                    .order_by(WorkflowApprovalRow.approval_id)
                    .limit(501)
                )
            ],
            "messages": [
                export(row)
                for row in session.scalars(
                    select(ConversationMessageRow)
                    .where(ConversationMessageRow.turn_id == turn.turn_id)
                    .order_by(ConversationMessageRow.message_id)
                    .limit(501)
                )
            ]
            if turn
            else [],
        }

    def _plan(self, session, root_id):
        """按根验证身份、输入、原始授权上界和唯一活动位置，绝不重新解析资料引用。"""
        facts = self._facts(session, root_id)
        plan = {
            "run_id": root_id,
            "fingerprint": digest(facts),
            "original": facts,
            "commands": {},
            "reasons": [],
            "state": "blocked",
        }
        if session.get(CoordinatedRunRow, root_id):
            plan["state"] = "already_coordinated"
            return plan
        turn = facts["turn"]
        if turn and turn["status"] in TERMINAL:
            plan["state"] = "historical_terminal"
            return plan
        reasons = plan["reasons"]
        if any(isinstance(value, list) and len(value) > 500 for value in facts.values()):
            reasons.append("legacy_evidence_volume_requires_manual_review")
            return plan
        if not turn:
            reasons.append("standalone_root_requires_explicit_business_mapping")
        executions = {item["run_id"]: item for item in facts["executions"]}
        if root_id not in executions:
            reasons.append("execution_snapshot_missing")
        if reasons:
            return plan
        context = snapshot_context(executions[root_id]["snapshot"])
        if (
            context.root_run_id != root_id
            or context.run_id != root_id
            or context.parent_run_id
            or context.conversation_id != turn["conversation_id"]
            or context.turn_id != turn["turn_id"]
            or context.tenant_id != turn["tenant_id"]
            or context.subject_id != turn["subject_id"]
            or executions[root_id]["snapshot"]["input_hash"] != turn["request_hash"]
        ):
            reasons.append("root_identity_or_input_mismatch")
        if session.scalar(
            select(ConversationTurnRow.run_id)
            .where(
                ConversationTurnRow.conversation_id == turn["conversation_id"],
                ConversationTurnRow.run_id != root_id,
                ConversationTurnRow.status.not_in(TERMINAL),
            )
            .limit(1)
        ):
            reasons.append("multiple_active_roots")
        for run_id, execution in executions.items():
            snapshot = execution["snapshot"]
            try:
                self.releases.verify(snapshot)
                child_context = snapshot_context(snapshot)
                if (
                    snapshot.get("driver_mode", "legacy") != "legacy"
                    or not child_context.request_clock
                    or child_context.root_run_id != root_id
                    or child_context.run_id != run_id
                    or (
                        child_context.tenant_id,
                        child_context.subject_id,
                        child_context.conversation_id,
                    )
                    != (context.tenant_id, context.subject_id, context.conversation_id)
                    or (run_id != root_id and child_context.parent_run_id != root_id)
                ):
                    raise ExecutionConflict("legacy_execution_identity_mismatch")
                operations = [op for op in facts["operations"] if op["run_id"] == run_id]
                if len(operations) != 1:
                    raise ExecutionConflict(
                        "legacy_operation_history_requires_application_evidence"
                    )
                command = self.inspector.map_start(execution, operations[0])
                if command.task_id != run_id or command.root_task_id != root_id:
                    raise ExecutionConflict("legacy_command_owner_mismatch")
                plan["commands"][run_id] = command
            except (ExecutionConflict, ValueError, KeyError) as exc:
                reasons.append(reason(exc))
        root_command = plan["commands"].get(root_id)
        messages = [row for row in facts["messages"] if row["role"] == "user"]
        if root_command and (
            len(messages) != 1
            or root_command.input
            != {
                "messages": [{"role": "user", "content": messages[0]["content"]}],
            }
        ):
            reasons.append("original_user_input_mismatch")
        delegations = facts["delegations"]
        if len(delegations) > 1 or len(executions) != len(delegations) + 1:
            reasons.append("multiple_or_unmapped_child_positions")
        for delegation in delegations:
            child = executions.get(delegation["child_run_id"])
            if (
                child is None
                or delegation["parent_run_id"] != root_id
                or delegation["delivered_at"]
                or delegation["execution_snapshot"] is None
            ):
                reasons.append("delegation_requires_original_position_evidence")
        if len(facts["interactions"]) > 1 or any(
            row["response"] is not None for row in facts["interactions"]
        ):
            reasons.append("legacy_decision_requires_application_evidence")
        plan["reasons"] = sorted(set(reasons))
        if not reasons:
            plan["state"] = "shadow_required"
        return plan

    @staticmethod
    def public(plan):
        """审查结果只包含根 ID、状态、摘要和原因，不泄露冻结输入与身份。"""
        return {key: plan[key] for key in ("run_id", "fingerprint", "state", "reasons")}

    def inventory(self, *, after="", limit=100):
        """Keyset 分页覆盖缺快照的 Turn 与独立 Workflow，避免 OFFSET 漏根。"""
        if not 1 <= limit <= 500:
            raise ValueError("inventory limit must be between 1 and 500")
        with self.store.sessions() as session:
            roots = union(
                select(ConversationTurnRow.run_id),
                select(RunExecutionRow.root_run_id),
                select(WorkflowRunRow.run_id).where(
                    ~select(DelegationRow.delegation_id)
                    .where(DelegationRow.child_run_id == WorkflowRunRow.run_id)
                    .exists()
                ),
            ).subquery()
            ids = list(
                session.scalars(
                    select(roots.c[0])
                    .where(roots.c[0] > after)
                    .order_by(roots.c[0])
                    .limit(limit + 1)
                )
            )
            plans = []
            for root_id in ids[:limit]:
                try:
                    plans.append(self.public(self._plan(session, root_id)))
                except (ValueError, KeyError, ExecutionConflict):
                    plans.append(
                        {
                            "run_id": root_id,
                            "fingerprint": digest(self._facts(session, root_id)),
                            "state": "blocked",
                            "reasons": ["incomplete_legacy_evidence"],
                        }
                    )
            return {"items": plans, "next_cursor": ids[limit - 1] if len(ids) > limit else None}

    async def shadow(self, root_id):
        """只读本库与 backend；不落进度、不延长授权、不调用有副作用的旧查询。"""
        with self.store.sessions() as session:
            try:
                plan = self._plan(session, root_id)
            except (ExecutionConflict, ValueError, KeyError) as exc:
                facts = self._facts(session, root_id)
                plan = {
                    "run_id": root_id,
                    "fingerprint": digest(facts),
                    "original": facts,
                    "commands": {},
                    "reasons": [reason(exc)],
                    "state": "blocked",
                }
        plan["observations"] = {}
        if plan["state"] != "shadow_required":
            return plan
        facts = plan["original"]
        for execution in facts["executions"]:
            run_id = execution["run_id"]
            operation = next(op for op in facts["operations"] if op["run_id"] == run_id)
            try:
                observation = await self.inspector.observe(
                    plan["commands"][run_id], execution, operation
                )
                plan["observations"][run_id] = observation
            except Exception as exc:
                plan["reasons"].append(reason(exc))
        if not plan["reasons"]:
            self._validate_shadow(plan)
        plan["state"] = "blocked" if plan["reasons"] else "ready_for_reauthorization"
        plan["shadow_hash"] = digest(
            {
                key: value.model_dump(mode="json") if value else None
                for key, value in plan["observations"].items()
            }
        )
        plan["observed_at"] = now()
        return plan

    def _validate_shadow(self, plan):
        """原父委派与 child 位置必须同时成立，用户交互保持原实例与截止时间。"""
        facts, observations = plan["original"], plan["observations"]
        requests = [request for item in observations.values() if item for request in item.requests]
        for run_id, item in observations.items():
            command = plan["commands"][run_id]
            if item and (
                item.execution_ref.task_id != run_id
                or item.execution_ref.operation_id != command.operation_id
                or item.execution_ref.backend_instance_id != command.backend_instance_id
            ):
                plan["reasons"].append("original_attempt_identity_mismatch")
            if item and (
                item.status not in {"waiting", "completed", "failed"}
                or (item.status == "waiting" and len(item.requests) != 1)
            ):
                plan["reasons"].append("backend_position_not_quiescent_or_unique")
        for record in facts["delegations"]:
            child_execution = next(
                row for row in facts["executions"] if row["run_id"] == record["child_run_id"]
            )
            request = next(
                (
                    request
                    for request in requests
                    if isinstance(request, DelegationRequest)
                    and request.request_id == record["delegation_id"]
                ),
                None,
            )
            if (
                request is None
                or request.owner_task_id != record["parent_run_id"]
                or request.target != release_ref(child_execution["snapshot"])
                or request.target.kind != record["kind"]
                or request.target.target_id != record["target_id"]
                or request.target.version != record["target_version"]
                or not matches_arguments(request, record)
            ):
                plan["reasons"].append("original_delegation_checkpoint_mismatch")
            elif request:
                observation = observations[request.owner_task_id]
                try:
                    self.inspector.verify_delegation(
                        request,
                        observation.continuation_bindings[request.continuation_ref.continuation_id],
                        record,
                        child_execution,
                    )
                except (ExecutionConflict, KeyError, ValueError) as exc:
                    plan["reasons"].append(reason(exc))
            child = observations.get(record["child_run_id"])
            if record["completed_at"] and (
                child is None
                or child.status not in {"completed", "failed"}
                or (
                    record["output_payload"] is not None
                    and record["output_payload"] != child.result
                )
            ):
                plan["reasons"].append("original_child_terminal_evidence_mismatch")
        for pending in facts["interactions"]:
            request = next(
                (
                    request
                    for request in requests
                    if isinstance(request, InteractionRequest)
                    and request.request_id == pending["interaction_id"]
                ),
                None,
            )
            if (
                request is None
                or request.revision != pending["revision"]
                or request.owner_task_id != pending["owner_run_id"]
                or request.expires_at != datetime.fromisoformat(pending["expires_at"])
                or request.question != pending["question"]
            ):
                plan["reasons"].append("original_interaction_checkpoint_mismatch")
        # 任何发布请求都必须与精确 source、固定 release 和完整 binding 对应。
        for request in requests:
            observation = observations.get(request.owner_task_id)
            binding = observation.continuation_bindings.get(
                request.continuation_ref.continuation_id
            )
            if (
                request.source_execution_ref != observation.execution_ref
                or binding is None
                or digest(binding) != request.continuation_ref.binding_hash
            ):
                plan["reasons"].append("continuation_evidence_mismatch")

    async def adopt(self, root_id, *, fingerprint, control_revision, shadow_hash):
        """重新只读检查后提交 CAS；原语义和 ID 不变，新授权必须由原主体显式提供。"""
        plan = await self.shadow(root_id)
        if (
            plan["state"] != "ready_for_reauthorization"
            or plan["fingerprint"] != fingerprint
            or plan.get("shadow_hash") != shadow_hash
        ):
            raise ExecutionConflict("legacy evidence changed or adoption is blocked")
        return await asyncio.to_thread(self._adopt, plan, control_revision)

    def _adopt(self, plan, control_revision):
        """门闩 → 会话 → 根，原始归档与协议转换、责任、过期 grant 在同一事务。"""
        root_id, facts = plan["run_id"], plan["original"]
        with self.store.sessions.begin() as session:
            gate = session.scalar(
                select(CoordinationControlRow)
                .where(CoordinationControlRow.control_id == 1)
                .with_for_update(read=True)
            )
            if (
                not gate
                or gate.revision != control_revision
                or not gate.legacy_fenced
                or not gate.stopped_evidence_hash
                or not gate.dispatch_paused
                or not gate.admission_paused
            ):
                raise ExecutionConflict(
                    "stopped legacy producers and paused deployment are required"
                )
            session.execute(
                update(ConversationRow)
                .where(ConversationRow.conversation_id == facts["turn"]["conversation_id"])
                .values(updated_at=ConversationRow.updated_at)
            )
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == root_id)
                .values(run_id=root_id)
            )
            if digest(self._facts(session, root_id)) != plan["fingerprint"] or session.get(
                CoordinatedRunRow, root_id
            ):
                raise ExecutionConflict("legacy root changed during takeover")
            if now() - plan["observed_at"] > timedelta(seconds=30):
                raise ExecutionConflict("shadow observation expired; repeat read-only verification")
            session.add(
                LegacyAdoptionRow(
                    run_id=root_id,
                    fingerprint=plan["fingerprint"],
                    shadow_hash=plan["shadow_hash"],
                    control_revision=control_revision,
                    original=facts,
                )
            )
            root = CoordinatedRunRow(
                run_id=root_id,
                conversation_id=facts["turn"]["conversation_id"],
                backend_instance_id=self.store.backend_instance_id,
                driver_version=DRIVER_VERSION,
                projection={},
                revision=0,
            )
            session.add(root)
            for saved in facts["executions"]:
                execution = session.get(RunExecutionRow, saved["run_id"])
                execution.snapshot = {
                    **saved["snapshot"],
                    "driver_mode": "coordinator",
                    "backend_instance_id": self.store.backend_instance_id,
                }
            session.flush()
            original_context = snapshot_context(session.get(RunExecutionRow, root_id).snapshot)
            session.add(
                RunAuthorizationRow(
                    run_id=root_id,
                    scopes=sorted(original_context.scopes),
                    source="legacy_migration",
                    source_hash=plan["fingerprint"],
                    issued_at=now(),
                    expires_at=now(),
                    revoked=True,
                )
            )
            for saved in facts["operations"]:
                operation = session.get(RunOperationRow, saved["operation_id"])
                command = plan["commands"][operation.run_id]
                scopes = session.get(RunExecutionRow, operation.run_id).snapshot["context"][
                    "scopes"
                ]
                operation.request = {
                    "kind": "start",
                    "payload": command.model_dump(mode="json"),
                    "scopes": sorted(scopes),
                }
                operation.request_hash = digest(operation.request)
                observation = plan["observations"][operation.run_id]
                if observation:
                    reference = observation.execution_ref
                    session.add(
                        BackendAttemptRow(
                            operation_id=operation.operation_id,
                            run_id=root_id,
                            backend_instance_id=reference.backend_instance_id,
                            execution_hash=digest(reference.execution_id),
                            reference=reference.model_dump(mode="json"),
                        )
                    )
                    # 原生索引转换为中立 attempt 索引；完整旧命令与观察仍保存在不可变归档。
                    operation.server_run_id, operation.status, operation.result = (
                        operation.operation_id,
                        "submitted",
                        None,
                    )
                    session.get(
                        RunExecutionRow, operation.run_id
                    ).server_run_id = operation.operation_id
                    workflow = session.get(WorkflowRunRow, operation.run_id)
                    if workflow:
                        workflow.server_run_id = operation.operation_id
            for record in facts["delegations"]:
                row = session.get(DelegationRow, record["delegation_id"])
                request = plan["observations"][row.parent_run_id].requests[0]
                row.execution_snapshot = {
                    **row.execution_snapshot,
                    "coordination_request": request.model_dump(mode="json"),
                }
                row.child_server_run_id = session.get(
                    RunExecutionRow, row.child_run_id
                ).server_run_id
            pending_projection = []
            for observation in plan["observations"].values():
                if observation:
                    for request in observation.requests:
                        binding = observation.continuation_bindings[
                            request.continuation_ref.continuation_id
                        ]
                        CoordinationTransitions.continuation(session, root, request, binding)
                        if isinstance(request, InteractionRequest):
                            pending = session.get(PendingInteractionRow, request.request_id)
                            if pending:
                                pending_projection.append(
                                    interaction_projection(request, pending.status)
                                )
                                stored = {
                                    "coordination": request.model_dump(mode="json"),
                                    "action_hash": request.action_hash,
                                    "required_scope": request.point.required_scope,
                                }
                                if request.approval_id:
                                    stored["approval_id"] = request.approval_id
                                pending.source = (
                                    "workflow_approval" if request.approval_id else "coordinator"
                                )
                                pending.request, pending.server_run_id = (
                                    stored,
                                    request.source_execution_ref.operation_id,
                                )
                                pending.request_hash = digest(
                                    {
                                        "source": pending.source,
                                        "point_id": pending.point_id,
                                        "kind": pending.kind,
                                        "question": pending.question,
                                        "request": stored,
                                    }
                                )
            self.store.project(
                session,
                root,
                run_id=root_id,
                thread_id=session.get(RunExecutionRow, root_id).snapshot["thread_id"],
                status="interrupted",
                waiting_reason="authorization_required",
                pending_interactions=pending_projection,
            )
            self.store.command_inbox(session, root, "legacy-adoption:" + plan["fingerprint"])
            return {
                "run_id": root_id,
                "driver_version": DRIVER_VERSION,
                "state": "authorization_required",
                "fingerprint": plan["fingerprint"],
            }
