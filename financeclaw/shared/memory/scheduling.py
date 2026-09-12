"""Owner-locked consolidation wakeups and contiguous disposition progress."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import owner_filter
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryOwnerRow
from financeclaw.shared.outbox.tables import OutboxEventRow


def ensure_consolidation_in_session(
    session: Session,
    owner: MemoryOwnerRow,
    *,
    delay_seconds: float = 5,
    pipeline_version: str = "memory/1",
    model_profile_version: str = "default",
) -> str | None:
    """Bind at most one live event while holding the owner's serialization lock."""
    if owner.active_consolidation_event_id:
        active = session.get(OutboxEventRow, owner.active_consolidation_event_id)
        if active is not None and active.status in {"pending", "publishing"}:
            return active.event_id
        owner.active_consolidation_event_id = None
    actor = MemoryActor(tenant_id=owner.tenant_id, subject_id=owner.subject_id)
    pending = session.scalars(
        select(MemoryExtractionRow.extraction_id)
        .where(
            *owner_filter(MemoryExtractionRow, actor),
            MemoryExtractionRow.disposition == "pending",
            MemoryExtractionRow.extraction_revision.is_not(None),
        )
        .limit(1)
    ).first()
    if pending is None or not owner.auto_enabled:
        return None
    event_id = f"memory-consolidate-{uuid4().hex}"
    session.add(
        OutboxEventRow(
            event_id=event_id,
            destination="memory_consolidate",
            event_type="memory.consolidate.requested",
            aggregate_type="memory_owner",
            aggregate_id=owner.subject_id,
            tenant_id=owner.tenant_id,
            subject_id=owner.subject_id,
            payload={
                "pipeline_version": pipeline_version,
                "model_profile_version": model_profile_version,
                "schema_version": "memory-v1",
                "requested_revision": owner.extraction_revision,
            },
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC) + timedelta(seconds=min(max(0, delay_seconds), 15)),
        )
    )
    owner.active_consolidation_event_id = event_id
    return event_id


def advance_consolidated_revision_in_session(session: Session, owner: MemoryOwnerRow) -> None:
    """Advance only across fully disposed ready groups; source order is unrelated."""
    session.flush()
    actor = MemoryActor(tenant_id=owner.tenant_id, subject_id=owner.subject_id)
    first_pending = session.scalars(
        select(MemoryExtractionRow.extraction_revision)
        .where(
            *owner_filter(MemoryExtractionRow, actor),
            MemoryExtractionRow.disposition == "pending",
            MemoryExtractionRow.extraction_revision.is_not(None),
        )
        .order_by(MemoryExtractionRow.extraction_revision)
        .limit(1)
    ).first()
    owner.consolidated_revision = max(
        owner.consolidated_revision,
        owner.extraction_revision if first_pending is None else first_pending - 1,
    )
