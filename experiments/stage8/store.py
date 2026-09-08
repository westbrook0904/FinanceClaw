"""共享业务事务实验：复用正式 Journal／操作日志，仅实验投影和责任使用独立表。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from experiments.stage8.backend import release
from financeclaw.kernel.coordination import BackendNotification, TaskSubmission, bounded_digest
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, ExecutionRepository
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase


class ExperimentBase(DeclarativeBase):
    """不注册进产品 Base，不产生 Alembic 迁移。"""


class Projection(ExperimentBase):
    """实验状态投影，主键复用 run_executions；不是新的 Task 身份。"""

    __tablename__ = "stage8_projection"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    driver_version: Mapped[int] = mapped_column(Integer, default=1)
    grant_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Inbox(ExperimentBase):
    """先提交后唤醒，未关联通知保留最小线索。"""

    __tablename__ = "stage8_inbox"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ProgressEvent(ExperimentBase):
    """完成事务中的业务事件，不与 callback 或桥接进度共用消费标记。"""

    __tablename__ = "stage8_events"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


class Lease(ExperimentBase):
    """PostgreSQL 支撑的持久到期责任。"""

    __tablename__ = "stage8_pg_leases"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    wake: Mapped[int] = mapped_column(Integer, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(128))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped: Mapped[bool] = mapped_column(Boolean, default=False)


class StaleWriter(RuntimeError):
    """租约、revision 或 driver 版本已失效，旧推进者不能写入。"""


def now() -> datetime:
    """实验的 UTC 物理时钟；生产授权时钟仍需可信服务策略。"""
    return datetime.now(UTC)


def fault(step: str, fail_at: str | None, session: Session) -> None:
    """Flush 后注入崩溃，验证真实 SQL 已执行仍会被外层事务回滚。"""
    session.flush()
    if step == fail_at:
        raise RuntimeError("injected transaction failure: " + step)


class ProbeStore:
    """以共享业务事实验证 Coordination 的基础推进和事务组合。"""

    def __init__(self, url: str) -> None:
        self.db = ApplicationDatabase(url)
        self.sessions = self.db.session_factory
        self.journal = SqlAlchemyConversationRepository(self.sessions)
        self.execution = ExecutionRepository(self.sessions)

    def initialize(self) -> None:
        """只能指向实验数据库或测试临时库；不改用户数据库。"""
        self.db.initialize_schema()
        ExperimentBase.metadata.create_all(self.db.engine)

    def _event(self, session: Session, root: Projection, kind: str) -> None:
        """在状态提交的同一事务追加业务事件。"""
        session.add(ProgressEvent(run_id=root.run_id, revision=root.revision, kind=kind))

    def wake(self, session: Session, root: Projection, event_id: str, kind: str) -> None:
        """唤醒不删除有效租约，不复制 BFF→Coordinator outbox。"""
        session.execute(
            insert(Inbox)
            .values(event_id=event_id, run_id=root.run_id, kind=kind, payload={})
            .on_conflict_do_nothing()
        )
        session.execute(
            insert(Lease)
            .values(run_id=root.run_id, due=now())
            .on_conflict_do_update(
                index_elements=[Lease.run_id], set_={"wake": Lease.wake + 1, "due": now()}
            )
        )

    def admit(
        self,
        conversation_id: str,
        *,
        key: str = "turn",
        ttl: float = 120,
        fail_at: str | None = None,
        lose_receipt: bool = False,
        unresolved_receipt: bool = False,
    ) -> str:
        """Turn、Journal、冻结输入／授权／start、inbox 和初始投影原子受理。"""
        with self.sessions.begin() as session:
            turn, _, replay = self.journal.begin_turn(
                conversation_id=conversation_id,
                tenant_id="probe",
                subject_id="probe",
                idempotency_key=conversation_id + key,
                request_hash=bounded_digest([conversation_id, key]),
                message="synthetic probe",
                target_type="agent",
                target_id="probe_parent",
                target_version="1.0.0",
                session=session,
            )
            if replay:
                return turn.run_id
            fault("journal", fail_at, session)
            payload = {
                "task_id": turn.run_id,
                "request_id": "request:" + turn.run_id,
                "turn_id": turn.turn_id,
                "conversation_id": conversation_id,
            }
            command = TaskSubmission(
                task_id=turn.run_id,
                root_task_id=turn.run_id,
                operation_id="start:" + turn.run_id,
                backend_instance_id="probe",
                release=release("parent"),
                input=payload,
                input_hash=bounded_digest(payload),
            )
            expires = now() + timedelta(seconds=ttl)
            self.execution.register(
                turn.run_id,
                {
                    "release": release("parent").model_dump(),
                    "input": payload,
                    "grant_until": expires.isoformat(),
                    "scopes": ["probe:run"],
                },
                session=session,
            )
            fault("snapshot", fail_at, session)
            self.execution.prepare(
                command.operation_id, turn.run_id, command.model_dump(mode="json"), session=session
            )
            fault("operation", fail_at, session)
            if unresolved_receipt:
                self.execution.claim(command.operation_id, session=session)
                self.execution.uncertain(command.operation_id, session=session)
            root = Projection(
                run_id=turn.run_id,
                grant_until=expires,
                revision=1,
                payload={
                    "stage": "root_start",
                    "command": command.model_dump(mode="json"),
                    "lose_receipt": lose_receipt,
                },
            )
            session.add(root)
            fault("projection", fail_at, session)
            self.wake(session, root, "start:" + turn.run_id, "command")
            fault("inbox", fail_at, session)
            self._event(session, root, "task.accepted")
            fault("event", fail_at, session)
            return turn.run_id

    def create_conversation(self) -> str:
        """创建隔离的合成会话，受理失败不会删除这个先前存在的会话。"""
        return self.journal.create_conversation(
            tenant_id="probe",
            subject_id="probe",
            agent_id="probe_parent",
            agent_profile_version="1.0.0",
        ).conversation_id

    def read(self, run_id: str) -> dict[str, Any]:
        """只读投影；验收观察此方法不会推进任何业务状态。"""
        with self.sessions() as session:
            root = session.get(Projection, run_id)
            return {
                "run_id": root.run_id,
                "revision": root.revision,
                "grant_until": root.grant_until,
                "cancelled": root.cancelled,
                "driver_version": root.driver_version,
                "payload": root.payload,
            }

    @contextmanager
    def locked(
        self,
        run_id: str,
        claim: dict[str, Any] | None = None,
    ) -> Iterator[tuple[Session, Projection]]:
        """固定锁序：Conversation→root execution→projection→lease→相关操作。"""
        with self.sessions.begin() as session:
            session.scalar(
                select(ConversationRow)
                .where(
                    ConversationRow.conversation_id
                    == select(ConversationTurnRow.conversation_id)
                    .where(ConversationTurnRow.run_id == run_id)
                    .scalar_subquery()
                )
                .with_for_update()
            )
            session.scalar(
                select(RunExecutionRow).where(RunExecutionRow.run_id == run_id).with_for_update()
            )
            root = session.scalar(
                select(Projection).where(Projection.run_id == run_id).with_for_update()
            )
            if root.driver_version != 1:
                raise StaleWriter("unsupported driver version")
            if claim is not None:
                lease = session.scalar(
                    select(Lease).where(Lease.run_id == run_id).with_for_update()
                )
                if (
                    lease.owner != claim["owner"]
                    or lease.epoch != claim["epoch"]
                    or lease.until <= now()
                    or lease.stopped
                ):
                    raise StaleWriter("expired or superseded lease")
            yield session, root

    def update(
        self,
        before: dict[str, Any],
        payload: dict[str, Any],
        claim: dict[str, Any] | None = None,
        *,
        final: dict[str, Any] | None = None,
        fail_at: str | None = None,
    ) -> bool:
        """CAS 提交一个协调步骤；完成使用同一 Session 写正式 Journal 和操作证据。"""
        with self.locked(before["run_id"], claim) as (session, root):
            if root.revision != before["revision"]:
                return False
            if final:
                self.journal.append_assistant_message(
                    run_id=root.run_id,
                    content="Stage-8 synthetic root-child-root completed",
                    session=session,
                )
                fault("assistant", fail_at, session)
                self.execution.observe_in_session(
                    session,
                    final["operation_id"],
                    server_run_id=final["execution_id"],
                    result=final["result"],
                )
                fault("terminal", fail_at, session)
            root.payload = payload
            root.revision += 1
            fault("projection", fail_at, session)
            self._event(session, root, "task.completed" if final else "task.progressed")
            fault("event", fail_at, session)
            return True

    def command_claim(
        self,
        run_id: str,
        command: dict[str, Any],
        task_id: str,
        claim: dict[str, Any] | None,
    ) -> bool:
        """授权与唯一命令领取同事务；租约接管不会把 uncertain 退回 prepared。"""
        with self.locked(run_id, claim) as (session, root):
            if root.cancelled or root.grant_until <= now():
                raise ExecutionConflict("cancelled or authorization expired")
            if task_id != run_id and session.get(RunExecutionRow, task_id) is None:
                self.execution.register(
                    task_id, {"command": command}, root_run_id=run_id, session=session
                )
            self.execution.prepare(command["operation_id"], task_id, command, session=session)
            return self.execution.claim(command["operation_id"], session=session)

    def receipt(
        self, root_id: str, operation_id: str, reference: str | None, claim: dict[str, Any] | None
    ) -> None:
        """旧 worker 的回执写入也受 fencing；新 worker 可按原操作找回远端回执。"""
        with self.locked(root_id, claim) as (session, _):
            if reference:
                self.execution.bind(operation_id, reference, session=session)
            else:
                self.execution.uncertain(operation_id, session=session)

    def decision(self, run_id: str, *, cancel: bool = False) -> None:
        """实验用户入口：只记录显式回答或取消并唤醒，不直接调用 backend。"""
        with self.locked(run_id) as (session, root):
            if cancel:
                root.cancelled = True
                self.execution.request_cancel(run_id, session=session)
            else:
                root.payload = {**root.payload, "answer": {"scope": "synthetic"}}
            root.revision += 1
            self.wake(session, root, f"decision:{run_id}:{root.revision}", "decision")

    def notification(self, notification: BackendNotification) -> None:
        """原始回调只触发核对；重复、早到和迟到回调不能直接改业务状态。"""
        with self.sessions.begin() as session:
            operation = session.scalar(
                select(RunOperationRow).where(
                    RunOperationRow.server_run_id == notification.execution_id
                )
            )
            root_id = (
                session.get(RunExecutionRow, operation.run_id).root_run_id if operation else None
            )
            event_id = "callback:" + notification.payload_digest
            result = session.execute(
                insert(Inbox)
                .values(
                    event_id=event_id,
                    run_id=root_id,
                    kind="notification",
                    payload=notification.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
            if result.rowcount and root_id:
                session.execute(
                    insert(Lease)
                    .values(run_id=root_id, due=now())
                    .on_conflict_do_update(
                        index_elements=[Lease.run_id],
                        set_={"wake": Lease.wake + 1, "due": now()},
                    )
                )
