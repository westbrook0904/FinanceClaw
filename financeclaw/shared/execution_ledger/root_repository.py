"""短事务组合、精确操作绑定与持久协调责任；网络调用始终在事务外。"""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update

from financeclaw.kernel.backend import BackendExecutionRef, BackendNotification
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.run_tables import (
    RootRunRow,
    RunInboxRow,
    RunProgressEventRow,
)
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow

DRIVER_VERSION = 1


def now() -> datetime:
    """UTC 持久化时间。"""
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    """SQLite 使用同一 UTC 语义。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class StaleRunLease(ExecutionConflict):
    """租约或协议已失效；旧推进者必须丢弃本次写入。"""


class RootRunRepository:
    """Root leases and fact transactions scoped to the current BFF protocol."""

    driver_version = DRIVER_VERSION

    def __init__(self, sessions, *, backend_instance_id: str, journal=None):
        """复用正式 Journal／执行仓储；所有 role 连接同一业务库。"""
        self.sessions = sessions
        self.backend_instance_id = backend_instance_id
        self.journal = journal or SqlAlchemyConversationRepository(sessions)
        self.execution = self.journal.execution

    def require_schema(self) -> None:
        """所有正式角色必须先完成共享迁移，缺表时在受理之前停止启动。"""
        from sqlalchemy import inspect

        from financeclaw.shared.notifications.facts import require_schema

        require_schema(self.sessions)
        from financeclaw.shared.execution_ledger.run_tables import (
            RunAuthorizationRow,
        )

        with self.sessions() as session:
            inspector = inspect(session.get_bind())
            for table in (
                RootRunRow,
                RunOperationRow,
                RunAuthorizationRow,
                RunInboxRow,
                RunProgressEventRow,
            ):
                if not inspector.has_table(table.__tablename__):
                    raise RuntimeError("application database migration is required")
                actual = {column["name"] for column in inspector.get_columns(table.__tablename__)}
                if not set(table.__table__.columns.keys()).issubset(actual):
                    raise RuntimeError("incompatible application database schema")

    def lock(self, session, root_id: str, claim=None) -> RootRunRow:
        """业务变更先取得会话与根锁，再校验 fencing；领取本身只锁协调行。"""
        row = session.get(RootRunRow, root_id)
        if row is None:
            raise ExecutionConflict("run is not registered")
        session.execute(
            update(ConversationRow)
            .where(ConversationRow.conversation_id == row.conversation_id)
            .values(updated_at=ConversationRow.updated_at)
        )
        session.execute(
            update(RunExecutionRow).where(RunExecutionRow.run_id == root_id).values(run_id=root_id)
        )
        row = session.scalar(
            select(RootRunRow)
            .where(RootRunRow.run_id == root_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row.driver_version != self.driver_version
            or row.backend_instance_id != self.backend_instance_id
        ):
            raise StaleRunLease("incompatible BFF run binding")
        if claim is not None and (
            row.owner != claim["owner"]
            or row.epoch != claim["epoch"]
            or row.lease_until is None
            or aware(row.lease_until) <= now()
        ):
            raise StaleRunLease("BFF run lease expired")
        return row

    @staticmethod
    def wake(row) -> None:
        """新唤醒不清除已有有效租约；旧 finish 必须保留更大的序号。"""
        row.wake += 1
        row.due_at = now()

    def command_inbox(self, session, row, key: str) -> None:
        """命令事实与推进责任同事务，不在 BFF 返回后补写。"""
        identity = digest([row.run_id, "command", key])
        if session.get(RunInboxRow, identity) is None:
            session.add(
                RunInboxRow(
                    inbox_id=identity,
                    kind="command",
                    run_id=row.run_id,
                    backend_instance_id=row.backend_instance_id,
                    payload={"operation_id": key},
                    expires_at=now() + timedelta(days=7),
                )
            )
        self.wake(row)

    def project(self, session, row, **changes) -> None:
        """只有安全业务状态改变才增加 revision；最终消息继续以 Journal 为准。"""
        projection = {**row.projection, **changes}
        if projection == row.projection:
            return
        row.projection, row.revision, row.updated_at = projection, row.revision + 1, now()
        from financeclaw.shared.notifications.facts import record_progress

        record_progress(session, row)
        safe_event = {
            "run_id": row.run_id,
            "status": projection["status"],
            "waiting_reason": projection.get("waiting_reason"),
            "interaction_ids": [
                item["interaction_id"] for item in projection.get("pending_interactions", ())
            ],
        }
        session.add(
            RunProgressEventRow(run_id=row.run_id, revision=row.revision, payload=safe_event)
        )
        from financeclaw.shared.audit.models import AuditEventType, AuditRecord
        from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
        from financeclaw.shared.execution_ledger.repository import snapshot_context

        context = snapshot_context(session.get(RunExecutionRow, row.run_id).snapshot)
        SqlAlchemyAuditRepository(self.sessions).append_in_session(
            session,
            AuditRecord(
                audit_id="audit-" + digest([row.run_id, row.revision]),
                event_type=AuditEventType.RUN_PROGRESS_UPDATED,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=row.conversation_id,
                turn_id=context.turn_id,
                run_id=row.run_id,
                resource_type="run",
                resource_id=row.run_id,
                resource_version=str(self.driver_version),
                action="advance",
                decision=projection["status"],
                policy_version="bff-runs/1",
                payload_hash=digest(safe_event),
            ),
        )

    def authorization_event(self, session, row, grant, decision):
        """授权依据只保存摘要，授权变化与永久 Audit／Outbox 同事务。"""
        from financeclaw.shared.audit.models import AuditEventType, AuditRecord
        from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
        from financeclaw.shared.execution_ledger.repository import snapshot_context

        context = snapshot_context(session.get(RunExecutionRow, row.run_id).snapshot)
        SqlAlchemyAuditRepository(self.sessions).append_in_session(
            session,
            AuditRecord(
                audit_id="audit-" + digest([row.run_id, "grant", grant.revision]),
                event_type=AuditEventType.RUN_AUTHORIZED,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=row.conversation_id,
                turn_id=context.turn_id,
                run_id=row.run_id,
                resource_type="run_authorization",
                resource_id=row.run_id,
                resource_version=str(grant.revision),
                action="authorize",
                decision=decision,
                policy_version="bff-runs/1",
                payload_hash=digest(
                    [grant.source_hash, grant.scopes, grant.expires_at.isoformat(), grant.revoked]
                ),
            ),
        )

    def notify(self, notification: BackendNotification) -> bool:
        """认证后的通知仅唤醒已绑定的任务；早到通知落库等待精确绑定。"""
        if notification.backend_instance_id != self.backend_instance_id:
            raise ValueError("notification backend does not match ingress deployment")
        identity = digest(
            [notification.backend_instance_id, notification.event_id or notification.payload_digest]
        )
        execution_hash = digest(notification.execution_id)
        with self.sessions.begin() as session:
            if session.get(RunInboxRow, identity) is not None:
                return False
            attempt = session.scalar(
                select(RunOperationRow).where(
                    RunOperationRow.backend_instance_id == self.backend_instance_id,
                    RunOperationRow.execution_hash == execution_hash,
                )
            )
            row = self.lock(session, attempt.run_id) if attempt else None
            if row is None:
                # 未知尝试独立限额；锁不与任何根业务锁组合。
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": int(digest([self.backend_instance_id, "inbox"])[0:15], 16)},
                    )
                pending = session.scalar(
                    select(func.count())
                    .select_from(RunInboxRow)
                    .where(
                        RunInboxRow.backend_instance_id == self.backend_instance_id,
                        RunInboxRow.run_id.is_(None),
                        RunInboxRow.processed.is_(False),
                        RunInboxRow.expires_at > now(),
                    )
                )
                if pending >= 1024:
                    raise ExecutionConflict("unassociated notification capacity exhausted")
            # SAVEPOINT 处理并发重复回调，不回滚已经存在的业务事务。
            from sqlalchemy.exc import IntegrityError

            from financeclaw.shared.execution_ledger.repository import _ensure_savepoint_transaction

            _ensure_savepoint_transaction(session)
            try:
                with session.begin_nested():
                    event = RunInboxRow(
                        inbox_id=identity,
                        kind="backend_notification",
                        run_id=None,
                        backend_instance_id=self.backend_instance_id,
                        execution_hash=execution_hash,
                        payload=notification.model_dump(mode="json"),
                        received_at=notification.received_at,
                        expires_at=now() + timedelta(days=7),
                    )
                    session.add(event)
                    session.flush()
            except IntegrityError:
                return False
            if attempt is not None:
                event.run_id = row.run_id
                if row.active:
                    self.wake(row)
                else:
                    event.processed = True
            return True

    def claim_due(
        self,
        owner: str,
        *,
        lease_seconds: float,
        maximum_inflight: int = 32,
        tenant_inflight: int = 4,
    ) -> dict[str, Any] | None:
        """短事务使用 SKIP LOCKED，只领取到期活跃责任，随即释放锁。"""
        from sqlalchemy.orm import aliased

        with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                # 跨进程的容量检查与领取共用短锁，不持有网络 I/O。
                if not session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {
                        "key": int(digest([self.backend_instance_id, "capacity"])[0:15], 16),
                    },
                ):
                    return None
            leased = (
                select(func.count())
                .select_from(RootRunRow)
                .where(
                    RootRunRow.backend_instance_id == self.backend_instance_id,
                    RootRunRow.lease_until > now(),
                )
            )
            if session.scalar(leased) >= maximum_inflight:
                return None
            other, conversation = aliased(RootRunRow), aliased(ConversationRow)
            tenant_leased = (
                select(func.count())
                .select_from(other)
                .join(conversation, conversation.conversation_id == other.conversation_id)
                .where(
                    other.backend_instance_id == self.backend_instance_id,
                    other.lease_until > now(),
                    conversation.tenant_id == ConversationRow.tenant_id,
                )
                .correlate(ConversationRow)
                .scalar_subquery()
            )
            row = session.scalar(
                select(RootRunRow)
                .join(
                    ConversationRow,
                    ConversationRow.conversation_id == RootRunRow.conversation_id,
                )
                .where(
                    tenant_leased < tenant_inflight,
                    RootRunRow.backend_instance_id == self.backend_instance_id,
                    RootRunRow.driver_version == self.driver_version,
                    RootRunRow.active.is_(True),
                    RootRunRow.due_at <= now(),
                    or_(
                        RootRunRow.lease_until.is_(None),
                        RootRunRow.lease_until <= now(),
                    ),
                )
                .order_by(RootRunRow.due_at)
                .limit(1)
                .with_for_update(skip_locked=True, of=RootRunRow)
            )
            if row is None:
                return None
            row.owner, row.epoch = owner, row.epoch + 1
            row.lease_until = now() + timedelta(seconds=lease_seconds)
            return {"run_id": row.run_id, "owner": owner, "epoch": row.epoch, "wake": row.wake}

    def renew(self, claim, *, lease_seconds: float) -> bool:
        """只有仍有效的原 epoch 可续租，过期不能通过续租抢回所有权。"""
        with self.sessions.begin() as session:
            result = session.execute(
                update(RootRunRow)
                .where(
                    RootRunRow.run_id == claim["run_id"],
                    RootRunRow.owner == claim["owner"],
                    RootRunRow.epoch == claim["epoch"],
                    RootRunRow.lease_until > now(),
                    RootRunRow.driver_version == self.driver_version,
                )
                .values(lease_until=now() + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1

    def finish(self, claim, *, delay: float, error: str | None = None) -> None:
        """旧责任不能覆盖新 wake；异常仅记录错误类别，不落远端敏感载荷。"""
        with self.sessions.begin() as session:
            row = self.lock(session, claim["run_id"], claim)
            row.failures = row.failures + 1 if error else 0
            row.last_error = error
            row.due_at = now() if row.wake != claim["wake"] else now() + timedelta(seconds=delay)
            row.owner, row.lease_until = None, None
            session.execute(
                update(RunInboxRow)
                .where(
                    RunInboxRow.run_id == row.run_id,
                    RunInboxRow.processed.is_(False),
                )
                .values(processed=True)
            )

    def bind(self, claim, reference: BackendExecutionRef, *, session=None) -> None:
        """回执与原操作匹配后原子绑定；既有 server_run_id 在协调模式保存尝试索引键。"""
        with nullcontext(session) if session is not None else self.sessions.begin() as session:
            row = self.lock(session, claim["run_id"], claim)
            operation = session.get(RunOperationRow, reference.operation_id)
            execution = session.get(RunExecutionRow, reference.task_id)
            if (
                reference.backend_instance_id != row.backend_instance_id
                or operation is None
                or operation.run_id != reference.task_id
                or execution.root_run_id != row.run_id
            ):
                raise ExecutionConflict("backend receipt does not match fixed operation")
            payload = reference.model_dump(mode="json")
            if operation.reference is not None and operation.reference != payload:
                raise ExecutionConflict("operation receipt cannot be replaced")
            operation.backend_instance_id = row.backend_instance_id
            operation.execution_hash = digest(reference.execution_id)
            operation.reference = payload
            self.execution.bind(reference.operation_id, reference.operation_id, session=session)
            session.execute(
                update(RunInboxRow)
                .where(
                    RunInboxRow.backend_instance_id == row.backend_instance_id,
                    RunInboxRow.execution_hash == digest(reference.execution_id),
                    RunInboxRow.run_id.is_(None),
                )
                .values(run_id=row.run_id)
            )
            self.wake(row)

    def reconcile_inbox(self) -> None:
        """补偿回调与回执交错提交；有界关联或清理，通知永不创建业务任务。"""
        with self.sessions() as session:
            bindings = list(
                session.execute(
                    select(RunInboxRow.inbox_id, RunOperationRow.run_id)
                    .select_from(RunInboxRow)
                    .join(
                        RunOperationRow,
                        (
                            (RunOperationRow.backend_instance_id == RunInboxRow.backend_instance_id)
                            & (RunOperationRow.execution_hash == RunInboxRow.execution_hash)
                        ),
                    )
                    .join(RootRunRow, RootRunRow.run_id == RunOperationRow.run_id)
                    .where(
                        RootRunRow.driver_version == self.driver_version,
                        RunInboxRow.backend_instance_id == self.backend_instance_id,
                        RunInboxRow.run_id.is_(None),
                        RunInboxRow.processed.is_(False),
                    )
                    .limit(100)
                )
            )
        for inbox_id, root_id in bindings:
            with self.sessions.begin() as session:
                row = self.lock(session, root_id)
                event = session.get(RunInboxRow, inbox_id)
                if event and event.run_id is None and not event.processed:
                    event.run_id, event.processed = root_id, not row.active
                    if row.active:
                        self.wake(row)
        with self.sessions.begin() as session:
            session.execute(
                delete(RunInboxRow).where(
                    RunInboxRow.backend_instance_id == self.backend_instance_id,
                    RunInboxRow.run_id.is_(None),
                    RunInboxRow.expires_at < now(),
                )
            )
