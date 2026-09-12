"""Independent memory-candidate notices bound to an already verified channel address."""

from sqlalchemy import select

from financeclaw.shared.memory.tables import MemorySourceRow
from financeclaw.shared.notifications.facts import target_valid
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow
from financeclaw.shared.turns.types import digest


def record_candidate(session, record):
    """Freeze a proposal view in the memory transaction without touching its task card."""
    if record.status != "proposed":
        return
    source_turns = select(MemorySourceRow.turn_id).where(
        MemorySourceRow.tenant_id == record.tenant_id,
        MemorySourceRow.subject_id == record.subject_id,
        MemorySourceRow.source_id.in_(record.evidence_source_ids),
    )
    targets = session.scalars(
        select(NotificationTargetRow)
        .where(
            NotificationTargetRow.tenant_id == record.tenant_id,
            NotificationTargetRow.subject_id == record.subject_id,
            NotificationTargetRow.turn_id.in_(source_turns),
            NotificationTargetRow.active.is_(True),
        )
        .order_by(NotificationTargetRow.created_at.desc())
        .limit(8)
    )
    target = next((item for item in targets if target_valid(session, item)), None)
    if target is None:
        return  # The candidate remains available through the authenticated management API.
    event_id = digest([target.target_id, "memory_candidate", record.memory_id, record.revision])
    if session.get(NotificationEventRow, event_id) is not None:
        return
    session.add(
        NotificationEventRow(
            event_id=event_id,
            target_id=target.target_id,
            revision=record.revision,
            kind="memory_candidates",
            payload={
                "candidate_id": record.memory_id,
                "revision": record.revision,
                "content_hash": record.content_hash,
                "content": record.content,
                "operation": record.operation,
                "field": record.field,
                "scope_type": record.scope_type,
                "scope_id": record.scope_id,
                "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            },
        )
    )
