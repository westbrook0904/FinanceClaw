"""Audit repository port plus deterministic test/development implementation."""

from collections.abc import Iterable
from threading import Lock
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from financeclaw.outbox.tables import OutboxEventRow

from .models import AuditEventType, AuditRecord
from .tables import AuditRecordRow


class AuditRepository(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class InMemoryAuditRepository:
    """Append-only repository used by tests and local development composition."""

    def __init__(self, records: Iterable[AuditRecord] = ()) -> None:
        self._records = list(records)
        self._lock = Lock()

    def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        with self._lock:
            return tuple(self._records)


class SqlAlchemyAuditRepository:
    """Append-only AuditRepository backed by the application database."""

    def __init__(self, sessions: sessionmaker, *, emit_outbox: bool = True) -> None:
        self._sessions = sessions
        self._emit_outbox = emit_outbox

    def append(self, record: AuditRecord) -> None:
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
        """Return a deterministic projection for tests and audit export jobs."""

        statement = select(AuditRecordRow)
        if tenant_id is not None:
            statement = statement.where(AuditRecordRow.tenant_id == tenant_id)
        if subject_id is not None:
            statement = statement.where(AuditRecordRow.subject_id == subject_id)
        statement = statement.order_by(AuditRecordRow.occurred_at, AuditRecordRow.audit_id)
        with self._sessions() as session:
            return tuple(_record(row) for row in session.scalars(statement))


def _record(row: AuditRecordRow) -> AuditRecord:
    """Project storage values back into the stable audit domain model."""

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
