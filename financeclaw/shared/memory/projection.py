"""Bounded deterministic SQL digest and immutable semantic-index event projections."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import canonical_hash, owner_filter
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow
from financeclaw.shared.outbox.tables import OutboxEventRow

INDEX_VERSION = "memory-v1"


def rebuild_digest_in_session(session: Session, owner: MemoryOwnerRow) -> None:
    """Project identifiers and bounded summaries; facts remain in memory_records."""
    session.flush()
    actor = MemoryActor(tenant_id=owner.tenant_id, subject_id=owner.subject_id)
    rows = tuple(
        session.scalars(
            select(MemoryRecordRow)
            .where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.is_current.is_(True),
                MemoryRecordRow.status == "active",
            )
            .order_by(MemoryRecordRow.owner_revision.desc(), MemoryRecordRow.memory_id)
            .limit(32)
        )
    )
    owner.digest = {
        "revision": owner.memory_revision,
        "profile": [
            {"memory_id": row.memory_id, "revision": row.revision, "field": row.field}
            for row in rows
            if row.kind == "profile"
        ],
        "tasks": [
            {"memory_id": row.memory_id, "revision": row.revision, "summary": row.content[:160]}
            for row in rows
            if row.kind == "task"
        ][:12],
    }


def enqueue_index_in_session(
    session: Session, owner: MemoryOwnerRow, row: MemoryRecordRow, *, delete: bool = False
) -> None:
    """Persist version-specific index maintenance atomically with fact visibility."""
    if not delete and (row.kind != "task" or row.status != "active"):
        return
    action = "delete" if delete else "requested"
    event_id = "memory-index-" + canonical_hash(
        [owner.tenant_id, owner.subject_id, row.memory_id, row.revision, action]
    )
    if session.get(OutboxEventRow, event_id):
        return
    session.add(
        OutboxEventRow(
            event_id=event_id,
            event_type=f"memory.index.{action}",
            destination="memory_index_delete" if delete else "memory_index",
            aggregate_type="memory",
            aggregate_id=row.memory_id,
            tenant_id=owner.tenant_id,
            subject_id=owner.subject_id,
            payload={
                "memory_id": row.memory_id,
                "revision": row.revision,
                "index_version": INDEX_VERSION,
                "privacy_epoch": owner.privacy_epoch,
            },
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC),
        )
    )
