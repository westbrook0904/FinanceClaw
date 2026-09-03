"""审计记录的仓储层实现。

位于 audit 模块的持久化边界：审计记录与 Outbox 事件在同一数据库事务中落盘，
保证每条永久审计记录都有对应的事件可投递，并提供内存实现用于测试。
"""

from collections.abc import Iterable
from threading import Lock
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from financeclaw.modules.outbox.tables import OutboxEventRow

from .models import AuditEventType, AuditRecord
from .tables import AuditRecordRow


class AuditRepository(Protocol):
    """审计仓储协议，约束审计实现必须提供的追加能力。

    使用场景：由审计相关服务依赖注入使用；实现需保证 ``append`` 返回即代表
    审计记录已持久化成功，按结构化子类型满足本协议即可。
    """

    def append(self, record: AuditRecord) -> None:
        """追加一条审计记录，持久化成功后方法才返回。"""
        ...


class InMemoryAuditRepository:
    """基于进程内列表的审计仓储实现，仅用于测试。

    使用场景：单元测试中收集审计记录并断言其内容；通过内部锁保证并发追加与
    读取的一致性。

    Attributes:
        _records: 已追加的审计记录列表（内部状态）。
        _lock: 保护 ``_records`` 的线程锁（内部状态）。

    """

    def __init__(self, records: Iterable[AuditRecord] = ()) -> None:
        """创建内存仓储，可选用既有记录初始化。

        Args:
            records: 初始审计记录序列，默认为空。

        """
        self._records = list(records)
        self._lock = Lock()

    def append(self, record: AuditRecord) -> None:
        """在锁保护下追加一条审计记录。

        Args:
            record: 待追加的审计记录。

        Raises:
            TypeError: 传入对象不是 ``AuditRecord`` 时抛出。

        """
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        """在锁保护下返回已追加审计记录的只读快照。"""
        with self._lock:
            return tuple(self._records)


class SqlAlchemyAuditRepository:
    """基于 SQLAlchemy 的审计仓储实现，审计与 Outbox 事件同事务落盘。

    使用场景：生产环境记录永久审计；``emit_outbox`` 开启时在追加审计的同一
    事务中写入一条 pending 状态的 Outbox 事件，保证记录与事件原子一致。

    Attributes:
        _sessions: SQLAlchemy 会话工厂，用于创建读写审计数据的数据库会话（内部状态）。
        _emit_outbox: 是否随审计记录同事务写入 Outbox 事件（内部状态）。

    """

    def __init__(self, sessions: sessionmaker, *, emit_outbox: bool = True) -> None:
        """创建审计仓储。

        Args:
            sessions: 用于创建数据库会话的 SQLAlchemy 会话工厂。
            emit_outbox: 是否在追加审计的同一事务中写入 Outbox 事件，默认开启。

        """
        self._sessions = sessions
        self._emit_outbox = emit_outbox

    def append(self, record: AuditRecord) -> None:
        """在单一数据库事务中持久化审计记录，并按需写入 Outbox 事件。

        Args:
            record: 待追加的审计记录。

        Raises:
            TypeError: 传入对象不是 ``AuditRecord`` 时抛出。

        """
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
        # 1. 把审计记录映射为 ORM 行，与 Outbox 事件写入同一事务，保证原子落盘。
        with self._sessions.begin() as session:
            session.add(
                AuditRecordRow(
                    audit_id=record.audit_id,
                    event_type=record.event_type.value,
                    occurred_at=record.occurred_at,
                    tenant_id=record.tenant_id,
                    subject_id=record.subject_id,
                    conversation_id=record.conversation_id,
                    turn_id=record.turn_id,
                    run_id=record.run_id,
                    tool_call_id=record.tool_call_id,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    resource_version=record.resource_version,
                    action=record.action,
                    decision=record.decision,
                    policy_version=record.policy_version,
                    payload_hash=record.payload_hash,
                    evidence_refs=list(record.evidence_refs),
                    artifact_refs=list(record.artifact_refs),
                    metadata_json=record.metadata,
                )
            )
            # 2. 需要投递时，写入一条 pending 的 Outbox 事件，供发布器异步分发。
            if self._emit_outbox:
                session.add(
                    OutboxEventRow(
                        event_id=f"outbox-{record.audit_id}",
                        event_type=record.event_type.value,
                        aggregate_type=record.resource_type,
                        aggregate_id=record.resource_id,
                        tenant_id=record.tenant_id,
                        subject_id=record.subject_id,
                        payload={
                            "audit_id": record.audit_id,
                            "run_id": record.run_id,
                            "event_type": record.event_type.value,
                            "resource_type": record.resource_type,
                            "resource_id": record.resource_id,
                            "payload_hash": record.payload_hash,
                        },
                        status="pending",
                        attempts=0,
                        available_at=record.occurred_at,
                        created_at=record.occurred_at,
                    )
                )

    def records(
        self, *, tenant_id: str | None = None, subject_id: str | None = None
    ) -> tuple[AuditRecord, ...]:
        """按租户/主体过滤查询审计记录，按发生时间升序返回。

        Args:
            tenant_id: 可选租户过滤条件，为 None 时不限租户。
            subject_id: 可选主体过滤条件，为 None 时不限主体。

        Returns:
            按发生时间与审计标识升序排列的审计记录元组。

        """
        # 1. 按可选的租户/主体条件逐步追加过滤。
        statement = select(AuditRecordRow)
        if tenant_id is not None:
            statement = statement.where(AuditRecordRow.tenant_id == tenant_id)
        if subject_id is not None:
            statement = statement.where(AuditRecordRow.subject_id == subject_id)
        # 2. 以发生时间与审计标识稳定排序后查询。
        statement = statement.order_by(AuditRecordRow.occurred_at, AuditRecordRow.audit_id)
        with self._sessions() as session:
            return tuple(_record(row) for row in session.scalars(statement))


def _record(row: AuditRecordRow) -> AuditRecord:
    """把 ORM 行 ``AuditRecordRow`` 转换为领域模型 ``AuditRecord``。"""
    return AuditRecord(
        audit_id=row.audit_id,
        event_type=AuditEventType(row.event_type),
        occurred_at=row.occurred_at,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        run_id=row.run_id,
        tool_call_id=row.tool_call_id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        resource_version=row.resource_version,
        action=row.action,
        decision=row.decision,
        policy_version=row.policy_version,
        payload_hash=row.payload_hash,
        evidence_refs=tuple(row.evidence_refs),
        artifact_refs=tuple(row.artifact_refs),
        metadata=row.metadata_json,
    )
