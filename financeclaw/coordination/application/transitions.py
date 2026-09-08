"""Coordinator 组合领域事务；委派、交互、Workflow 与 Journal 继续复用原事实。"""

from sqlalchemy import select

from financeclaw.coordination.application.admission import prepare_task
from financeclaw.coordination.delegation.repository import SqlAlchemyDelegationRepository
from financeclaw.coordination.interactions.repository import InteractionRepository
from financeclaw.coordination.repository import now
from financeclaw.coordination.workflows.repository import SqlAlchemyWorkflowRepository
from financeclaw.kernel.coordination import DelegationRequest, InteractionRequest, ResponseDelivery
from financeclaw.kernel.delegation.models import DelegationKind, DelegationResult, DelegationStatus
from financeclaw.kernel.workflows.models import (
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowRunStatus,
)
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
from financeclaw.shared.execution_ledger.authorization import check_authorization, require_scopes
from financeclaw.shared.execution_ledger.coordination_tables import ContinuationRow
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.tables import RunExecutionRow
from financeclaw.shared.infrastructure.security.redaction import redact_sensitive


class CoordinationTransitions:
    """由 Worker 在取得有效根租约后组合短事务；外部输入解析在事务外完成。"""

    def __init__(self, store, releases):
        """现有领域仓储共享 Session、Audit 和 outbox。"""
        self.store, self.releases = store, releases
        self.delegations = SqlAlchemyDelegationRepository(store.sessions)
        self.interactions = InteractionRepository(store.execution)
        self.workflows = SqlAlchemyWorkflowRepository(store.sessions)
        self.audit = SqlAlchemyAuditRepository(store.sessions)

    def event(self, session, root, resource_id, event_type, decision):
        """只记录稳定 ID 和摘要，不复制输入、回答或完整 backend 输出。"""
        context = snapshot_context(session.get(RunExecutionRow, root.run_id).snapshot)
        self.audit.append_in_session(
            session,
            AuditRecord(
                audit_id="audit-" + digest([resource_id, event_type, decision]),
                event_type=event_type,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                run_id=root.run_id,
                resource_type="coordination",
                resource_id=resource_id,
                resource_version="1",
                action="advance",
                decision=decision,
                policy_version="coordination/1",
                payload_hash=digest([resource_id, decision]),
            ),
        )

    def bind(self, claim, reference):
        """确切回执、原 child 绑定、运行状态与 Audit 共同提交。"""
        from financeclaw.shared.execution_ledger.tables import RunOperationRow

        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            self.store.bind(claim, reference, session=session)
            if session.get(RunExecutionRow, root.run_id).cancellation_requested:
                return
            operation = session.get(RunOperationRow, reference.operation_id)
            if reference.task_id != root.run_id:
                record = session.scalar(
                    select(DelegationRow).where(DelegationRow.child_run_id == reference.task_id)
                )
                if record is None:
                    raise ExecutionConflict("child receipt has no accepted delegation")
                if operation.request["kind"] == "start":
                    self.delegations.bind_child(
                        record.delegation_id,
                        child_run_id=reference.task_id,
                        child_thread_id=record.child_thread_id,
                        child_server_run_id=reference.operation_id,
                        status=DelegationStatus.RUNNING,
                        session=session,
                    )
                    if record.kind == "workflow":
                        self.workflows.bind_server_run(
                            reference.task_id, reference.operation_id, "running", session=session
                        )
                    self.event(
                        session,
                        root,
                        record.delegation_id,
                        AuditEventType.DELEGATION_STARTED,
                        "submitted",
                    )
                else:
                    self.delegations.set_status(
                        record.delegation_id, DelegationStatus.RUNNING, session=session
                    )
                    if record.kind == "workflow":
                        self.workflows.set_status(
                            reference.task_id, WorkflowRunStatus.RUNNING, session=session
                        )
            self.store.journal.update_turn_status(root.run_id, "running", session=session)
            self.store.project(
                session, root, status="running", waiting_reason=None, pending_interactions=[]
            )

    @staticmethod
    def continuation(session, root, request, binding):
        """请求与原生等待位置一同提交，原绑定不可覆盖。"""
        reference = request.continuation_ref
        row = session.get(ContinuationRow, reference.continuation_id)
        if row is not None:
            if row.reference != reference.model_dump(mode="json") or row.binding != binding:
                raise ExecutionConflict("continuation was reused for different evidence")
            return
        if digest(binding) != reference.binding_hash:
            raise ExecutionConflict("continuation binding digest differs")
        session.add(
            ContinuationRow(
                continuation_id=reference.continuation_id,
                run_id=root.run_id,
                request_id=request.request_id,
                reference=reference.model_dump(mode="json"),
                binding=binding,
            )
        )

    def delegate(self, claim, request, binding, prepared_child):
        """唯一委派、固定 child、start 操作、父等待、Audit 与唤醒原子受理。"""
        snapshot, payload, arguments = prepared_child
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            parent = session.get(RunExecutionRow, request.owner_task_id)
            if request.owner_task_id != root.run_id:
                raise ExecutionConflict("recursive delegation is not enabled")
            if (
                parent.server_run_id != request.source_execution_ref.operation_id
                or parent.cancellation_requested
                or parent.side_effects_denied
            ):
                raise ExecutionConflict("parent no longer permits this delegation")
            grant = check_authorization(session, parent)
            require_scopes(grant.scopes, self.releases.verify(snapshot).required_scopes)
            existing = session.get(DelegationRow, request.request_id)
            if existing:
                if existing.execution_snapshot.get("coordination_request") != request.model_dump(
                    mode="json"
                ):
                    raise ExecutionConflict("delegation request identity was reused")
                return
            context = snapshot_context(snapshot)
            record, _ = self.delegations.ensure_requested(
                delegation_id=request.request_id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                parent_turn_id=context.turn_id,
                parent_run_id=request.owner_task_id,
                kind=DelegationKind(request.target.kind),
                target_id=request.target.target_id,
                target_version=request.target.version,
                arguments=arguments,
                execution_snapshot={"coordination_request": request.model_dump(mode="json")},
                session=session,
            )
            session.flush()
            if request.target.kind == "workflow":
                definition = self.releases.verify(snapshot)
                workflow, _ = self.workflows.begin_run(
                    definition=definition,
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    idempotency_key=request.request_id,
                    arguments_hash=snapshot["input_hash"],
                    request_fingerprint=digest([request.request_id, payload]),
                    input_payload=payload,
                    session=session,
                )
                context = context.model_copy(update={"run_id": workflow.run_id})
                snapshot = {
                    **snapshot,
                    "thread_id": workflow.thread_id,
                    "context": context.model_dump(mode="json"),
                }
            self.store.execution.register(
                context.run_id, snapshot, root_run_id=root.run_id, session=session
            )
            self.delegations.bind_child(
                record.delegation_id,
                child_run_id=context.run_id,
                child_thread_id=snapshot["thread_id"],
                child_server_run_id=None,
                status=DelegationStatus.PENDING,
                session=session,
            )
            self.continuation(session, root, request, binding)
            prepare_task(self.store, session, root, snapshot, payload, grant.scopes)
            self.event(
                session, root, request.request_id, AuditEventType.DELEGATION_REQUESTED, "accepted"
            )
            self.store.project(
                session,
                root,
                status="running",
                waiting_reason="delegation_pending",
                pending_interactions=[],
            )

    def interaction(self, claim, request: InteractionRequest, binding):
        """一个根最多一个待用户交互；原问题、决定期限、Workflow 镜像同事务保存。"""
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            self.continuation(session, root, request, binding)
            execution = session.get(RunExecutionRow, request.owner_task_id)
            if execution.server_run_id != request.source_execution_ref.operation_id:
                raise ExecutionConflict("interaction observation is stale")
            source = "coordinator"
            stored = {
                "coordination": request.model_dump(mode="json"),
                "action_hash": request.action_hash,
                "required_scope": request.point.required_scope,
            }
            if request.approval_id:
                source = "workflow_approval"
                context = snapshot_context(execution.snapshot)
                action = request.action
                self.workflows.ensure_approval(
                    WorkflowApproval(
                        approval_id=request.approval_id,
                        run_id=context.run_id,
                        tenant_id=context.tenant_id,
                        subject_id=context.subject_id,
                        approval_point=action["approval_point"],
                        arguments_hash=execution.snapshot["input_hash"],
                        requested_action=action["requested_action"],
                        request_payload=action,
                        allowed_decisions=request.allowed_decisions,
                        required_scope=request.point.required_scope,
                        status=WorkflowApprovalStatus.PENDING,
                        requested_at=now(),
                        expires_at=request.expires_at,
                    ),
                    session=session,
                )
                stored["approval_id"] = request.approval_id
            row = self.interactions.register(
                request.owner_task_id,
                source=source,
                server_run_id=request.source_execution_ref.operation_id,
                interrupt_id=request.request_id,
                point_id=request.point.point_id,
                kind=request.point.kind,
                question=request.question,
                request=stored,
                expires_at=request.expires_at,
                now=now(),
                session=session,
                identifier=request.request_id,
            )
            # 原生位置留在 continuation；统一交互 ID 由已冻结的 request 决定。
            if row["interaction_id"] != request.request_id:
                raise ExecutionConflict("interaction identity mapping mismatch")
            if row["revision"] != request.revision:
                raise ExecutionConflict("interaction revision changed during observation")
            if request.owner_task_id != root.run_id:
                record = session.scalar(
                    select(DelegationRow).where(DelegationRow.child_run_id == request.owner_task_id)
                )
                self.delegations.set_status(
                    record.delegation_id, DelegationStatus.INTERRUPTED, session=session
                )
                if record.kind == "workflow":
                    self.workflows.set_status(
                        request.owner_task_id, WorkflowRunStatus.INTERRUPTED, session=session
                    )
            self.store.journal.update_turn_status(root.run_id, "interrupted", session=session)
            self.store.project(
                session,
                root,
                status="interrupted",
                waiting_reason=request.point.kind + "_required"
                if row["status"] == "pending"
                else "interaction_" + row["status"],
                pending_interactions=[
                    {
                        "interaction_id": request.request_id,
                        "revision": request.revision,
                        "kind": request.point.kind,
                        "status": row["status"],
                        "question": request.question,
                        "owner_run_id": request.owner_task_id,
                        "root_run_id": root.run_id,
                        "expires_at": request.expires_at.isoformat(),
                        "response_url": f"/v1/interactions/{request.request_id}/responses",
                        **(
                            {"response_schema": request.point.response_schema}
                            if request.point.kind == "input"
                            else {}
                        ),
                        **(
                            {"options": list(request.point.options)}
                            if request.point.kind == "choice"
                            else {}
                        ),
                        **(
                            {
                                "action_hash": request.action_hash,
                                "arguments_hash": request.action_hash,
                                "allowed_decisions": list(request.allowed_decisions),
                                "action": redact_sensitive(request.action),
                                "interrupt_id": request.request_id,
                            }
                            if request.point.kind == "approval"
                            else {}
                        ),
                    }
                ],
            )

    def child_completed(self, claim, observation):
        """保存 child 终态与交付命令；child 完成不等于 parent 已应用结果。"""
        reference = observation.execution_ref
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            if session.get(RunExecutionRow, root.run_id).cancellation_requested:
                return
            self.require_applied(session, observation)
            record = session.scalar(
                select(DelegationRow).where(DelegationRow.child_run_id == reference.task_id)
            )
            if record is None:
                raise ExecutionConflict("child is not bound to a delegation")
            request = DelegationRequest.model_validate(
                record.execution_snapshot["coordination_request"]
            )
            status = (
                DelegationStatus.COMPLETED
                if observation.status == "completed"
                else DelegationStatus.FAILED
            )
            if record.kind == "workflow" and observation.status == "completed":
                status = DelegationStatus(observation.result["status"])
            record_model, _ = self.delegations.set_status(
                record.delegation_id,
                status,
                output_payload=observation.result,
                error=(observation.result or {}).get("error")
                or ("backend_execution_failed" if status is DelegationStatus.FAILED else None),
                session=session,
            )
            self.store.execution.observe_in_session(
                session,
                reference.operation_id,
                server_run_id=reference.operation_id,
                result=observation.model_dump(mode="json"),
            )
            if record.kind == "workflow":
                self.workflows.set_status(
                    reference.task_id,
                    WorkflowRunStatus(status.value),
                    output_payload=observation.result,
                    session=session,
                )
            result = DelegationResult(
                delegation_id=record.delegation_id,
                kind=record.kind,
                target_id=record.target_id,
                target_version=record.target_version,
                child_run_id=reference.task_id,
                parent_run_id=record.parent_run_id,
                arguments_hash=request.input_hash,
                status=status.value,
                output=record_model.output_payload,
                error=record_model.error,
            )
            operation_id = "operation-" + digest(
                [record.parent_run_id, "delivery:" + record.delegation_id]
            )
            command = ResponseDelivery(
                operation_id=operation_id,
                request=request,
                response=result,
                responding_task_id=reference.task_id,
            )
            parent = session.get(RunExecutionRow, root.run_id)
            grant = check_authorization(session, parent)
            self.store.execution.prepare(
                operation_id,
                record.parent_run_id,
                {
                    "kind": "response",
                    "payload": command.model_dump(mode="json"),
                    "scopes": grant.scopes,
                    "predecessor": request.source_execution_ref.operation_id,
                },
                session=session,
            )
            self.store.command_inbox(session, root, operation_id)
            self.event(
                session,
                root,
                record.delegation_id,
                AuditEventType.DELEGATION_COMPLETED,
                status.value,
            )
            self.store.project(
                session,
                root,
                status="running",
                waiting_reason="delivery_pending",
                pending_interactions=[],
            )

    def applied(self, session, root, observation):
        """仅确切恢复操作的匹配证据能标记交付；父随后失败也保留真实交付事实。"""
        from financeclaw.shared.execution_ledger.tables import RunOperationRow

        for proof in observation.response_applications:
            operation = session.get(RunOperationRow, proof.operation_id)
            if operation is None or operation.request["kind"] != "response":
                raise ExecutionConflict("application evidence has no response operation")
            command = ResponseDelivery.model_validate(operation.request["payload"])
            if not proof.confirms(command, observation.execution_ref):
                raise ExecutionConflict("application evidence does not match exact response")
            row = session.get(ContinuationRow, proof.continuation_id)
            if row.applied_operation_id not in {None, proof.operation_id}:
                raise ExecutionConflict("continuation was already applied by another operation")
            if row.applied_operation_id:
                continue
            row.applied_operation_id, row.application_evidence = (
                proof.operation_id,
                proof.model_dump(mode="json"),
            )
            if isinstance(command.request, DelegationRequest):
                self.delegations.set_status(
                    command.request.request_id, DelegationStatus.DELIVERED, session=session
                )
                self.event(
                    session,
                    root,
                    command.request.request_id,
                    AuditEventType.DELEGATION_DELIVERED,
                    "applied",
                )

    @staticmethod
    def require_applied(session, observation):
        """恢复尝试的终态必须另有已应用原决定／结果的证据。"""
        from financeclaw.shared.execution_ledger.tables import RunOperationRow

        operation = session.get(RunOperationRow, observation.execution_ref.operation_id)
        if operation.request["kind"] == "response":
            command = ResponseDelivery.model_validate(operation.request["payload"])
            row = session.get(ContinuationRow, command.request.continuation_ref.continuation_id)
            if row is None or row.applied_operation_id != operation.operation_id:
                raise ExecutionConflict("response application is not confirmed")

    def root_completed(self, claim, observation):
        """Journal、操作终态、根进度与事件同进同退；摘要留给派生工作。"""
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            execution = session.get(RunExecutionRow, root.run_id)
            if execution.cancellation_requested or not root.active:
                return
            self.applied(session, root, observation)
            self.require_applied(session, observation)
            undelivered = session.scalar(
                select(DelegationRow).where(
                    DelegationRow.parent_run_id == root.run_id, DelegationRow.delivered_at.is_(None)
                )
            )
            if undelivered:
                self.store.project(
                    session,
                    root,
                    status="interrupted",
                    waiting_reason="delivery_application_unconfirmed",
                    pending_interactions=[],
                )
                return
            reference = observation.execution_ref
            self.store.execution.observe_in_session(
                session,
                reference.operation_id,
                server_run_id=reference.operation_id,
                result=observation.model_dump(mode="json"),
            )
            if observation.status == "completed":
                self.store.journal.append_assistant_message(
                    run_id=root.run_id, content=observation.result["message"], session=session
                )
            else:
                self.store.journal.update_turn_status(root.run_id, "failed", session=session)
            root.active = False
            self.store.project(
                session,
                root,
                status="completed" if observation.status == "completed" else "failed",
                waiting_reason=None,
                pending_interactions=[],
            )
