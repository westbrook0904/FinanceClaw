"""Protect original context while memory derivation or a reviewed candidate still needs it."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, cast, exists, func, select
from sqlalchemy.dialects.postgresql import JSONB

from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import owner_filter
from financeclaw.shared.memory.tables import (
    MemoryExtractionRow,
    MemoryOwnerRow,
    MemoryRecordRow,
    MemorySourceRow,
)
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow


def _references_conversation_sources(session, actor, conversation_id, column):
    """Use exact owner-scoped source references, never text matching inside a memory body."""
    if session.get_bind().dialect.name == "postgresql":
        contains = cast(column, JSONB).bool_op("?")(MemorySourceRow.source_id)
    else:
        elements = func.json_each(column).table_valued("value")
        contains = exists(
            select(1).select_from(elements).where(elements.c.value == MemorySourceRow.source_id)
        )
    return exists(
        select(MemorySourceRow.source_id).where(
            *owner_filter(MemorySourceRow, actor),
            MemorySourceRow.conversation_id == conversation_id,
            contains,
        )
    )


def memory_retention_reasons(session, conversation) -> tuple[str, ...]:
    """Read one stable owner snapshot after callers acquire any required conversation lock."""
    actor = MemoryActor(tenant_id=conversation.tenant_id, subject_id=conversation.subject_id)
    owner = session.scalars(
        select(MemoryOwnerRow)
        .where(*owner_filter(MemoryOwnerRow, actor))
        .with_for_update(read=True)
    ).one_or_none()
    if owner is None:
        return ()
    conversation_id = conversation.conversation_id
    now = datetime.now(UTC)
    reasons = []
    if session.scalar(
        select(OutboxEventRow.event_id)
        .where(
            *owner_filter(OutboxEventRow, actor),
            OutboxEventRow.destination == "memory_extract",
            OutboxEventRow.payload["conversation_id"].as_string() == conversation_id,
            OutboxEventRow.status.in_(["pending", "publishing", "dead_letter"]),
        )
        .limit(1)
    ):
        reasons.append("memory_extraction_job")
    if session.scalar(
        select(MemoryExtractionRow.extraction_id)
        .where(
            *owner_filter(MemoryExtractionRow, actor),
            MemoryExtractionRow.disposition == "pending",
            _references_conversation_sources(
                session, actor, conversation_id, MemoryExtractionRow.evidence_source_ids
            ),
        )
        .limit(1)
    ):
        reasons.append("memory_extraction_output")
    if session.scalar(
        select(MemoryRecordRow.memory_id)
        .where(
            *owner_filter(MemoryRecordRow, actor),
            MemoryRecordRow.is_current.is_(True),
            MemoryRecordRow.status == "proposed",
            MemoryRecordRow.expires_at > now,
            _references_conversation_sources(
                session, actor, conversation_id, MemoryRecordRow.evidence_source_ids
            ),
        )
        .limit(1)
    ):
        reasons.append("memory_candidate")
    expiry_text = MemorySourceRow.permit["expires_at"].as_string()
    unexpired = (
        cast(expiry_text, DateTime(timezone=True)) > now
        if session.get_bind().dialect.name == "postgresql"
        else func.datetime(expiry_text) > func.datetime(now.isoformat())
    )
    if session.get_bind().dialect.name == "postgresql":
        output_contains = cast(MemoryExtractionRow.evidence_source_ids, JSONB).bool_op("?")(
            MemorySourceRow.source_id
        )
    else:
        output_refs = func.json_each(MemoryExtractionRow.evidence_source_ids).table_valued("value")
        output_contains = exists(
            select(1)
            .select_from(output_refs)
            .where(output_refs.c.value == MemorySourceRow.source_id)
        )
    disposed = exists(
        select(MemoryExtractionRow.extraction_id).where(
            *owner_filter(MemoryExtractionRow, actor),
            MemoryExtractionRow.disposition.in_(["consumed", "quarantined"]),
            output_contains,
        )
    )
    skipped = exists(
        select(OutboxEventRow.event_id).where(
            *owner_filter(OutboxEventRow, actor),
            OutboxEventRow.destination == "memory_extract",
            OutboxEventRow.payload["turn_id"].as_string() == MemorySourceRow.turn_id,
            OutboxEventRow.status == "published",
            OutboxEventRow.event_type.in_(["memory.extract.prepare", "memory.extract.skipped"]),
            OutboxEventRow.processing_metadata["outcome"].as_string().not_in(["prepared"]),
        )
    )
    if owner.auto_enabled and session.scalar(
        select(MemorySourceRow.source_id)
        .join(ConversationTurnRow, ConversationTurnRow.turn_id == MemorySourceRow.turn_id)
        .where(
            *owner_filter(MemorySourceRow, actor),
            MemorySourceRow.conversation_id == conversation_id,
            ConversationTurnRow.status == "completed",
            MemorySourceRow.visible.is_(True),
            MemorySourceRow.version_valid.is_(True),
            MemorySourceRow.reuse_blocked.is_(False),
            MemorySourceRow.permit_revoked.is_(False),
            MemorySourceRow.permit.is_not(None),
            MemorySourceRow.permit["policy_revision"].as_integer() == owner.policy_revision,
            unexpired,
            ~disposed,
            ~skipped,
        )
        .limit(1)
    ):
        reasons.append("memory_source_unprocessed")
    return tuple(reasons)
