"""提供内存及 SQLAlchemy 审计事件仓储。"""

from collections.abc import Iterable
from threading import Lock
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from financeclaw.modules.outbox.tables import OutboxEventRow

from .models import AuditEventType, AuditRecord
from .tables import AuditRecordRow


class AuditRepository(Protocol):
    """定义审计Repository。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    def append(self, record: AuditRecord) -> None:
        """追加不可变审计事件；重复事件标识按仓储约定保持幂等。"""
        ...


class InMemoryAuditRepository:
    """定义In记忆审计Repository。

    适用场景：
        用于领域服务需要持久化状态，同时不应感知 SQL 细节的场景。

    属性：
        _records: 测试或内存实现持有的领域记录集合。
        _lock: 内部 `lock` 状态或依赖，不属于公开接口。
    """

    def __init__(self, records: Iterable[AuditRecord] = ()) -> None:
        """注入并保存审计事件所需的协作对象，同时校验构造期不变量。"""
        self._records = list(records)
        self._lock = Lock()

    def append(self, record: AuditRecord) -> None:
        """追加不可变审计事件；重复事件标识按仓储约定保持幂等。"""
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        """返回仓储当前保存记录的不可变快照，主要用于测试与诊断。"""
        with self._lock:
            return tuple(self._records)


class SqlAlchemyAuditRepository:
    """定义SqlAlchemy审计Repository。

    适用场景：
        用于领域服务需要持久化状态，同时不应感知 SQL 细节的场景。

    属性：
        _sessions: 内部 `sessions` 状态或依赖，不属于公开接口。
        _emit_outbox: 内部 `emit outbox` 状态或依赖，不属于公开接口。
    """

    def __init__(self, sessions: sessionmaker, *, emit_outbox: bool = True) -> None:
        """注入并保存审计事件所需的协作对象，同时校验构造期不变量。"""
        self._sessions = sessions
        self._emit_outbox = emit_outbox

    def append(self, record: AuditRecord) -> None:
        """追加不可变审计事件；重复事件标识按仓储约定保持幂等。"""
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
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
        """返回仓储当前保存记录的不可变快照，主要用于测试与诊断。"""
        statement = select(AuditRecordRow)
        if tenant_id is not None:
            statement = statement.where(AuditRecordRow.tenant_id == tenant_id)
        if subject_id is not None:
            statement = statement.where(AuditRecordRow.subject_id == subject_id)
        statement = statement.order_by(AuditRecordRow.occurred_at, AuditRecordRow.audit_id)
        with self._sessions() as session:
            return tuple(_record(row) for row in session.scalars(statement))


def _record(row: AuditRecordRow) -> AuditRecord:
    """把 ORM 行或 Store 条目转换为不可变领域记录。"""
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
