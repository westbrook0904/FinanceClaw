"""Database-backed claim/acknowledge operations for competing publishers."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import OutboxEvent, OutboxStatus
from .tables import OutboxEventRow


class OutboxRepository(Protocol):
    def claim_pending(self, *, limit: int, lease_seconds: int = 60) -> tuple[OutboxEvent, ...]: ...

    def mark_published(self, event_id: str) -> None: ...

    def mark_failed(self, event_id: str, error: str, *, max_attempts: int) -> None: ...


class SqlAlchemyOutboxRepository:
    """Lease rows with ``SKIP LOCKED`` so horizontally scaled workers do not duplicate work."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def claim_pending(self, *, limit: int, lease_seconds: int = 60) -> tuple[OutboxEvent, ...]:
        now = datetime.now(UTC)
        eligible = or_(
            OutboxEventRow.status == OutboxStatus.PENDING.value,
            (
                (OutboxEventRow.status == OutboxStatus.PUBLISHING.value)
                & (OutboxEventRow.locked_until < now)
            ),
        )
        statement = (
            select(OutboxEventRow)
            .where(eligible, OutboxEventRow.available_at <= now)
            .order_by(OutboxEventRow.available_at, OutboxEventRow.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        with self._sessions.begin() as session:
            rows = tuple(session.scalars(statement))
            for row in rows:
                row.status = OutboxStatus.PUBLISHING.value
                row.locked_until = now + timedelta(seconds=lease_seconds)
            return tuple(_event(row) for row in rows)

    def mark_published(self, event_id: str) -> None:
        with self._sessions.begin() as session:
            row = _owned_claim(session, event_id)
            row.status = OutboxStatus.PUBLISHED.value
            row.published_at = datetime.now(UTC)
            row.locked_until = None
            row.last_error = None

    def mark_failed(self, event_id: str, error: str, *, max_attempts: int) -> None:
        with self._sessions.begin() as session:
            row = _owned_claim(session, event_id)
            row.attempts += 1
            row.last_error = error[:1_000]
            row.locked_until = None
            if row.attempts >= max_attempts:
                row.status = OutboxStatus.DEAD_LETTER.value
            else:
                row.status = OutboxStatus.PENDING.value
                delay_seconds = min(300, 2 ** min(row.attempts, 8))
                row.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)


def _owned_claim(session: Session, event_id: str) -> OutboxEventRow:
    row = session.get(OutboxEventRow, event_id)
    if row is None or row.status != OutboxStatus.PUBLISHING.value:
        raise LookupError("outbox event is not owned by this publisher lease")
    return row


def _event(row: OutboxEventRow) -> OutboxEvent:
    return OutboxEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        payload=row.payload,
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        available_at=row.available_at,
        locked_until=row.locked_until,
        created_at=row.created_at,
        published_at=row.published_at,
        last_error=row.last_error,
    )
