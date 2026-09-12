"""Owner-filtered SQL reads and short owner-row serialization primitives."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.shared.memory.models import (
    MemoryActor,
    MemoryNotFound,
    MemoryOwnerSnapshot,
    MemoryPermissionError,
    MemoryRecord,
    MemorySource,
    utc,
)
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow, MemorySourceRow


def canonical_hash(value: Any) -> str:
    """Hash canonical JSON for stable receipt/source identity across process retries."""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def content_hash(content: str) -> str:
    """Hash exact original text without normalization or truncation."""
    return hashlib.sha256(content.encode()).hexdigest()


def owner_filter(row, actor: MemoryActor):
    """Build the exact tenant and subject predicate before any body is selected."""
    return (row.tenant_id == actor.tenant_id, row.subject_id == actor.subject_id)


def lock_owner(session: Session, actor: MemoryActor) -> MemoryOwnerRow:
    """Create if absent and lock owner; callers acquire required business locks first."""
    values = dict(
        tenant_id=actor.tenant_id,
        subject_id=actor.subject_id,
        memory_revision=0,
        source_seq=0,
        extraction_revision=0,
        consolidated_revision=0,
        policy_revision=1,
        privacy_epoch=0,
        read_enabled=True,
        auto_enabled=True,
        digest={},
    )
    dialect = session.get_bind().dialect.name
    insert = (
        pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    )
    if insert is None:
        raise RuntimeError("memory authority requires PostgreSQL (SQLite is for unit tests)")
    session.execute(
        insert(MemoryOwnerRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["tenant_id", "subject_id"])
    )
    return session.scalars(
        select(MemoryOwnerRow)
        .where(*owner_filter(MemoryOwnerRow, actor))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def source_snapshot(row: MemorySourceRow) -> MemorySource:
    """Detach a reference-only source snapshot from the SQL session."""
    return MemorySource.model_validate(row)


def record_snapshot(row: MemoryRecordRow) -> MemoryRecord:
    """Detach immutable fact content and its exact evidence versions."""
    record = MemoryRecord.model_validate(row)
    if row.status == "proposed" and row.expires_at and utc(row.expires_at) <= datetime.now(UTC):
        return record.model_copy(update={"status": "expired"})
    return record


def current_record(session: Session, actor: MemoryActor, memory_id: str) -> MemoryRecordRow | None:
    """Select a head only after restricting ownership in SQL."""
    return session.scalars(
        select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor),
            MemoryRecordRow.memory_id == memory_id,
            MemoryRecordRow.is_current.is_(True),
        )
    ).one_or_none()


def record_status_filter(status: str):
    """Project deadline expiry without mutating a candidate during a read request."""
    now = datetime.now(UTC)
    if status == "proposed":
        return and_(
            MemoryRecordRow.status == "proposed",
            or_(MemoryRecordRow.expires_at.is_(None), MemoryRecordRow.expires_at > now),
        )
    if status == "expired":
        return or_(
            MemoryRecordRow.status == "expired",
            and_(MemoryRecordRow.status == "proposed", MemoryRecordRow.expires_at <= now),
        )
    return MemoryRecordRow.status == status


class MemoryRepository:
    """Read authoritative memory, rechecking source visibility even for frozen revisions."""

    def __init__(self, sessions: sessionmaker):
        """Use the existing synchronous application database session factory."""
        self.sessions = sessions

    def owner_snapshot(self, actor: MemoryActor) -> MemoryOwnerSnapshot:
        """Read defaults without inserting a meaningless owner row on every query."""
        with self.sessions() as session:
            return self.owner_snapshot_in_session(session, actor)

    def owner_snapshot_in_session(
        self, session: Session, actor: MemoryActor
    ) -> MemoryOwnerSnapshot:
        """Read current owner revision/policy in the caller's transaction."""
        row = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        return (
            MemoryOwnerSnapshot.model_validate(row)
            if row
            else MemoryOwnerSnapshot(tenant_id=actor.tenant_id, subject_id=actor.subject_id)
        )

    def get(
        self,
        actor: MemoryActor,
        memory_id: str,
        *,
        revision: int | None = None,
        include_candidates: bool = False,
    ) -> MemoryRecord:
        """Read one exact authorized revision; omitted revision means current head."""
        with self.sessions() as session:
            return self.get_in_session(
                session, actor, memory_id, revision=revision, include_candidates=include_candidates
            )

    def get_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        memory_id: str,
        *,
        revision: int | None = None,
        include_candidates: bool = False,
    ) -> MemoryRecord:
        """Reject hidden, deleted, foreign-scope or unsupported source bodies."""
        self._require_read(session, actor)
        statement = select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor), MemoryRecordRow.memory_id == memory_id
        )
        statement = (
            statement.where(MemoryRecordRow.revision == revision)
            if revision is not None
            else statement.where(MemoryRecordRow.is_current.is_(True))
        )
        row = session.scalars(statement).one_or_none()
        if row is None or not self._readable(session, actor, row, include_candidates):
            raise MemoryNotFound("memory is absent or no longer readable")
        return record_snapshot(row)

    def list_records(
        self,
        actor: MemoryActor,
        *,
        kind: str | None = None,
        status: str = "active",
        scope_type: str | None = None,
        scope_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """Return a bounded owner-scoped keyset page of source-validated facts."""
        with self.sessions() as session:
            return self.list_records_in_session(
                session,
                actor,
                kind=kind,
                status=status,
                scope_type=scope_type,
                scope_id=scope_id,
                limit=limit,
                after=after,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )

    def list_records_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        *,
        kind: str | None = None,
        status: str = "active",
        scope_type: str | None = None,
        scope_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """Filter before retrieving bodies and retain bounded SQL work during degradation."""
        self._require_read(session, actor)
        actor = actor.model_copy(
            update={
                "agent_id": agent_id or actor.agent_id,
                "conversation_id": conversation_id or actor.conversation_id,
            }
        )
        statement = select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor),
            MemoryRecordRow.is_current.is_(True),
            record_status_filter(status),
        )
        if kind:
            statement = statement.where(MemoryRecordRow.kind == kind)
        if scope_type is not None:
            statement = statement.where(MemoryRecordRow.scope_type == scope_type)
        if scope_id is not None:
            statement = statement.where(MemoryRecordRow.scope_id == scope_id)
        if after:
            statement = statement.where(MemoryRecordRow.memory_id > after)
        rows = session.scalars(
            statement.order_by(MemoryRecordRow.memory_id).limit(max(1, min(limit, 100)))
        )
        return tuple(
            record_snapshot(row)
            for row in rows
            if self._readable(session, actor, row, status != "active")
        )

    def list_page(
        self,
        actor: MemoryActor,
        *,
        kind: str | None = None,
        status: str = "active",
        scope_type: str | None = None,
        scope_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[tuple[MemoryRecord, ...], str | None]:
        """Return source-validated records and the last scanned key, including empty pages."""
        with self.sessions() as session:
            return self.list_page_in_session(
                session,
                actor,
                kind=kind,
                status=status,
                scope_type=scope_type,
                scope_id=scope_id,
                limit=limit,
                after=after,
            )

    def list_page_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        *,
        kind: str | None = None,
        status: str = "active",
        scope_type: str | None = None,
        scope_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[tuple[MemoryRecord, ...], str | None]:
        """Keep keyset progress independent of filtering hidden or expired source records."""
        self._require_read(session, actor)
        count = max(1, min(limit, 100))
        statement = select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor),
            MemoryRecordRow.is_current.is_(True),
            record_status_filter(status),
        )
        if kind:
            statement = statement.where(MemoryRecordRow.kind == kind)
        if scope_type is not None:
            statement = statement.where(MemoryRecordRow.scope_type == scope_type)
        if scope_id is not None:
            statement = statement.where(MemoryRecordRow.scope_id == scope_id)
        if after:
            statement = statement.where(MemoryRecordRow.memory_id > after)
        rows = tuple(
            session.scalars(statement.order_by(MemoryRecordRow.memory_id).limit(count + 1))
        )
        page = rows[:count]
        records = tuple(
            record_snapshot(row)
            for row in page
            if self._readable(session, actor, row, status != "active")
        )
        return records, page[-1].memory_id if len(rows) > count else None

    def _require_read(self, session: Session, actor: MemoryActor) -> None:
        """Apply read permission and the latest explicit privacy setting."""
        from financeclaw.shared.memory.authorization import require_scope

        require_scope(actor, "memory:read")
        if not self.owner_snapshot_in_session(session, actor).read_enabled:
            raise MemoryPermissionError("memory reading is disabled")

    def _readable(
        self, session: Session, actor: MemoryActor, row: MemoryRecordRow, include_candidates: bool
    ) -> bool:
        """Frozen historical revisions remain readable only while current privacy allows."""
        if row.status != "active" and not (
            include_candidates
            and row.status in {"proposed", "approved", "rejected", "expired", "superseded"}
        ):
            return False
        head = current_record(session, actor, row.memory_id)
        if head is None or head.status in {"forgotten", "invalidated"}:
            return False
        if row.status == "active" and row.expires_at and utc(row.expires_at) <= datetime.now(UTC):
            return False
        if row.scope_type == "agent" and actor.kind != "user" and row.scope_id != actor.agent_id:
            return False
        if (
            row.scope_type == "conversation"
            and actor.kind != "user"
            and row.scope_id != actor.conversation_id
        ):
            return False
        from financeclaw.shared.memory.evidence import EvidenceReader

        try:
            EvidenceReader().read_in_session(
                session, actor, tuple(record_snapshot(row).evidence), for_derivation=False
            )
        except (MemoryPermissionError, MemoryNotFound, ValueError):
            return False
        return True
