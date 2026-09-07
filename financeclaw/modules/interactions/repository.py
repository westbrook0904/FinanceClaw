"""根任务树串行的交互事实：决定、审批镜像、操作准备及审计同事务提交。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from financeclaw.modules.audit import AuditEventType, AuditRecord, SqlAlchemyAuditRepository
from financeclaw.modules.execution import ExecutionConflict, ExecutionRepository, snapshot_context
from financeclaw.modules.execution.repository import digest
from financeclaw.modules.execution.tables import RunExecutionRow
from financeclaw.modules.workflows.tables import WorkflowApprovalRow

from .tables import PendingInteractionRow


class InteractionConflict(ExecutionConflict):
    """版本、回答、执行位置、权限或生命周期不允许继续。"""


class InteractionNotFound(LookupError):
    """交互不存在或不属于当前身份；对外不区分。"""


def aware(value: datetime) -> datetime:
    """SQLite 的朴素时间按持久化约定解释为 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def export(row: PendingInteractionRow) -> dict[str, Any]:
    """离开事务前脱离 ORM，JSON 只供可信应用层使用。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class InteractionRepository:
    """以根执行行为互斥点保存交互实例、用户决定和恢复命令。

    register 按 owner/server run/interrupt 三元组去重，轮询同一问题不会
    延长有效期。decide 在同一事务内写回答、准备出站操作、更新 Workflow
    审批镜像并追加 Audit/Outbox，避免回答已生效却缺失可恢复命令。

    返回值是供应用层使用的内部记录，公开接口需要另做字段投影。归属、
    时效和执行位置在本层复验；回答 Schema 与发布版本由应用服务校验。
    """

    def __init__(self, execution: ExecutionRepository) -> None:
        """决定、操作和审计使用同一个数据库会话工厂。"""
        self.execution = execution
        self.sessions = execution.sessions
        self.audit = SqlAlchemyAuditRepository(self.sessions)

    @staticmethod
    def _lock(session, root_run_id: str) -> RunExecutionRow:
        """根行是交互决定与取消的公共互斥位置，支持部署数据库行锁。"""
        # 写回相同主键是为了取得写锁；父、子运行必须锁同一 root，
        # 才能让跨渠道回答、交互替换与任务取消按同一顺序生效。
        session.execute(
            update(RunExecutionRow)
            .where(RunExecutionRow.run_id == root_run_id)
            .values(run_id=root_run_id)
        )
        root = session.get(RunExecutionRow, root_run_id)
        if root is None:
            raise InteractionConflict("root execution is unavailable")
        return root

    def _event(self, session, row, event_type, now) -> None:
        """Audit/Outbox 与事实同事务；只记录摘要和标识，不记录回答原文。"""
        context = snapshot_context(session.get(RunExecutionRow, row.owner_run_id).snapshot)
        self.audit.append_in_session(
            session,
            AuditRecord(
                audit_id="audit-" + digest([row.interaction_id, row.status, row.response_hash]),
                event_type=event_type,
                occurred_at=now,
                tenant_id=row.tenant_id,
                subject_id=row.subject_id,
                conversation_id=row.conversation_id,
                turn_id=context.turn_id,
                run_id=row.owner_run_id,
                resource_type="interaction",
                resource_id=row.interaction_id,
                resource_version=str(row.revision),
                action="respond" if row.response else "observe",
                decision=row.status,
                policy_version="interaction-policy/1.0.0",
                payload_hash=row.response_hash or row.request_hash,
                metadata={
                    "root_run_id": row.root_run_id,
                    "parent_run_id": row.parent_run_id,
                    "delegation_id": row.delegation_id,
                    "operation_id": row.operation_id,
                    "interrupt_id": row.interrupt_id,
                    "kind": row.kind,
                    "approval_id": row.request.get("approval_id"),
                },
            ),
        )

    def _close(self, session, row, status: str, now) -> None:
        """交互窗口结束不代表运行停止；Workflow 历史单保持同一事实。"""
        if row.status != "pending":
            return
        row.status = status
        row.decided_at = now
        approval_id = row.request.get("approval_id")
        if approval_id:
            approval = session.get(WorkflowApprovalRow, approval_id)
            if approval is not None and approval.status == "pending":
                approval.status = status
                approval.decided_at = now
        self._event(session, row, AuditEventType.INTERACTION_CLOSED, now)

    def register(
        self,
        owner_run_id: str,
        *,
        source: str,
        server_run_id: str,
        interrupt_id: str,
        point_id: str,
        kind: str,
        question: str,
        request: dict[str, Any],
        expires_at: datetime,
        now: datetime,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """同原生实例幂等；只有实际新位置才替换旧请求，不把轮询当作续期。"""
        execution = self.execution.get(owner_run_id)
        context = snapshot_context(execution["snapshot"])
        identifier = "interaction-" + digest([owner_run_id, server_run_id, interrupt_id])
        request_hash = digest(
            {
                "source": source,
                "point_id": point_id,
                "kind": kind,
                "question": question,
                "request": request,
            }
        )
        with self.sessions.begin() as session:
            root = self._lock(session, execution["root_run_id"])
            owner = session.get(RunExecutionRow, owner_run_id)
            if owner.server_run_id != server_run_id:
                raise InteractionConflict(
                    "stale interaction observation; query the current attempt"
                )
            previous = session.get(PendingInteractionRow, identifier)
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise InteractionConflict("native interaction changed without a new instance")
                if root.cancellation_requested:
                    self._close(session, previous, "cancelled", now)
                elif aware(previous.expires_at) <= now:
                    self._close(session, previous, "expired", now)
                return export(previous)
            if root.cancellation_requested:
                raise InteractionConflict("cancelled task cannot request new user interaction")
            for pending in session.scalars(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.root_run_id == root.run_id,
                    PendingInteractionRow.status == "pending",
                )
            ):
                if pending.owner_run_id != owner_run_id:
                    raise InteractionConflict(
                        "only one user interaction is supported per root task"
                    )
                self._close(session, pending, "superseded", now)
            session.flush()
            revision = (
                session.scalar(
                    select(func.max(PendingInteractionRow.revision)).where(
                        PendingInteractionRow.owner_run_id == owner_run_id
                    )
                )
                or 0
            ) + 1
            row = PendingInteractionRow(
                interaction_id=identifier,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                root_run_id=root.run_id,
                owner_run_id=owner_run_id,
                parent_run_id=context.parent_run_id,
                delegation_id=context.delegation_id,
                source=source,
                thread_id=execution["snapshot"]["thread_id"],
                server_run_id=server_run_id,
                interrupt_id=interrupt_id,
                checkpoint_id=checkpoint_id,
                point_id=point_id,
                revision=revision,
                kind=kind,
                question=question,
                request=request,
                request_hash=request_hash,
                status="pending",
                created_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            self._event(session, row, AuditEventType.INTERACTION_REQUESTED, now)
            if expires_at <= now:
                self._close(session, row, "expired", now)
            return export(row)

    def get_owned(
        self, identifier: str, tenant_id: str, subject_id: str, *, now: datetime
    ) -> dict[str, Any]:
        """按归属查询并惰性关闭过期窗口，不宣称远程已经停止。"""
        with self.sessions() as session:
            row = session.get(PendingInteractionRow, identifier)
            if row is None or (row.tenant_id, row.subject_id) != (tenant_id, subject_id):
                raise InteractionNotFound("interaction not found")
            root_id = row.root_run_id
        with self.sessions.begin() as session:
            root = self._lock(session, root_id)
            row = session.get(PendingInteractionRow, identifier)
            if root.cancellation_requested:
                self._close(session, row, "cancelled", now)
            elif now >= aware(row.expires_at):
                self._close(session, row, "expired", now)
            return export(row)

    def for_owner(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """按原生 owner 查询实例历史，仅供可信应用层使用。"""
        with self.sessions() as session:
            return tuple(
                export(row)
                for row in session.scalars(
                    select(PendingInteractionRow)
                    .where(PendingInteractionRow.owner_run_id == run_id)
                    .order_by(PendingInteractionRow.revision)
                )
            )

    def decide(
        self,
        identifier: str,
        *,
        tenant_id: str,
        subject_id: str,
        revision: int,
        response_key: str,
        response: dict[str, Any],
        operation: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """响应幂等身份、Schema 等在服务层校验；此处再次 CAS 验证时效和原生位置。"""
        current = self.get_owned(identifier, tenant_id, subject_id, now=now)
        fingerprint = digest(response)
        with self.sessions.begin() as session:
            root = self._lock(session, current["root_run_id"])
            row = session.get(PendingInteractionRow, identifier)
            if row.revision != revision:
                raise InteractionConflict("interaction revision does not match")
            if row.response is not None:
                if row.response_key != response_key or row.response_hash != fingerprint:
                    raise InteractionConflict("interaction already has a different response")
                return export(row)
            if (
                root.cancellation_requested
                or row.status != "pending"
                or now >= aware(row.expires_at)
            ):
                raise InteractionConflict("interaction is no longer pending")
            owner = session.get(RunExecutionRow, row.owner_run_id)
            if owner.server_run_id != row.server_run_id:
                raise InteractionConflict("interaction owner moved to another execution attempt")
            operation_id = "operation-" + digest([row.owner_run_id, "interaction:" + identifier])
            # 与决定共享事务，但尚未发送网络请求；进程此时退出后仍可通过
            # operation_id 找回冻结命令，而无需重新解释用户的回答。
            self.execution.prepare(operation_id, row.owner_run_id, operation, session=session)
            row.response_key, row.response_hash, row.response = response_key, fingerprint, response
            row.decided_at, row.decided_by, row.operation_id = now, subject_id, operation_id
            row.status = "rejected" if response.get("decision") == "reject" else "resolved"
            if row.status == "rejected":
                root.side_effects_denied = True
            approval_id = row.request.get("approval_id")
            if approval_id:
                approval = session.get(WorkflowApprovalRow, approval_id)
                if (
                    approval is None
                    or approval.run_id != row.owner_run_id
                    or approval.status != "pending"
                ):
                    raise InteractionConflict(
                        "Workflow approval already has a different terminal state"
                    )
                approval.status = "rejected" if row.status == "rejected" else "approved"
                approval.decided_at, approval.decided_by = now, subject_id
                approval.decision_reason = response.get("reason")
                # 保留原 Workflow 审计事件供现有订阅者消费；决定与两种审计
                # 同事务提交，重放在上方直接返回，不会再生成一次批准事件。
                self.audit.append_in_session(
                    session,
                    AuditRecord(
                        audit_id="audit-" + digest([identifier, "workflow-decision"]),
                        event_type=AuditEventType.WORKFLOW_APPROVED
                        if approval.status == "approved"
                        else AuditEventType.WORKFLOW_REJECTED,
                        occurred_at=now,
                        tenant_id=row.tenant_id,
                        subject_id=subject_id,
                        conversation_id=row.conversation_id,
                        turn_id=snapshot_context(owner.snapshot).turn_id,
                        run_id=row.owner_run_id,
                        resource_type="workflow_approval",
                        resource_id=approval_id,
                        resource_version=owner.snapshot["release"]["version"],
                        action="resume",
                        decision=approval.status,
                        policy_version="interaction-policy/1.0.0",
                        payload_hash=row.response_hash,
                        metadata={"interaction_id": identifier, "operation_id": operation_id},
                    ),
                )
            self._event(session, row, AuditEventType.INTERACTION_DECIDED, now)
            return export(row)

    def cancel_tree(self, root_run_id: str, *, now: datetime) -> None:
        """在封闭根执行的同一事务关闭待交互窗口；远程停止仍由取消协调器确认。"""
        with self.sessions.begin() as session:
            self._lock(session, root_run_id)
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.root_run_id == root_run_id)
                .values(cancellation_requested=True)
            )
            for row in session.scalars(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.root_run_id == root_run_id,
                    PendingInteractionRow.status == "pending",
                )
            ):
                self._close(session, row, "cancelled", now)
