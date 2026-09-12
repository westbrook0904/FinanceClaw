"""Privacy transitions invalidate SQL facts before asynchronous copies are purged."""

from datetime import UTC, datetime

from sqlalchemy import cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from financeclaw.shared.memory.models import MemoryActor, MemoryNotFound
from financeclaw.shared.memory.projection import enqueue_index_in_session, rebuild_digest_in_session
from financeclaw.shared.memory.repository import owner_filter
from financeclaw.shared.memory.tables import (
    MemoryExtractionRow,
    MemoryOwnerRow,
    MemoryRecordRow,
    MemorySourceRow,
)


def references_any(session: Session, column, source_ids: set[str]):
    """Use PostgreSQL GIN-compatible containment, with SQLite JSON semantics for units."""
    if session.get_bind().dialect.name == "postgresql":
        return or_(*(cast(column, JSONB).contains([source_id]) for source_id in source_ids))
    elements = func.json_each(column).table_valued("value")
    return exists(select(1).select_from(elements).where(elements.c.value.in_(source_ids)))


def clear_derivatives_in_session(
    session: Session, owner: MemoryOwnerRow, source_ids: set[str]
) -> None:
    """Quarantine whole mixed extraction bodies so retained fragments cannot resurrect facts."""
    if not source_ids:
        return
    actor = MemoryActor(tenant_id=owner.tenant_id, subject_id=owner.subject_id)
    rows = session.scalars(
        select(MemoryExtractionRow).where(
            *owner_filter(MemoryExtractionRow, actor),
            references_any(session, MemoryExtractionRow.evidence_source_ids, source_ids),
        )
    )
    for row in rows:
        row.output = {}
        row.disposition = "quarantined"
        row.disposition_reason = "source_privacy_invalidated"
        row.consumed_at = datetime.now(UTC)


def forget_record_in_session(
    session: Session, actor: MemoryActor, owner: MemoryOwnerRow, record: MemoryRecordRow
) -> None:
    """Erase historical fact bodies, block old source reuse and schedule all exact keys."""
    owner.privacy_epoch += 1
    versions = tuple(
        session.scalars(
            select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor), MemoryRecordRow.memory_id == record.memory_id
            )
        )
    )
    sources = {source_id for version in versions for source_id in version.evidence_source_ids}
    for source in session.scalars(
        select(MemorySourceRow).where(
            *owner_filter(MemorySourceRow, actor), MemorySourceRow.source_id.in_(sources)
        )
    ):
        source.reuse_blocked = True
        source.permit_revoked = True
    clear_derivatives_in_session(session, owner, sources)
    for old in session.scalars(
        select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor), MemoryRecordRow.memory_id == record.memory_id
        )
    ):
        enqueue_index_in_session(session, owner, old, delete=True)
        old.content = ""
    # A pending proposal using the forgotten source cannot keep its mixed original body.
    for candidate in (
        session.scalars(
            select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.is_current.is_(True),
                MemoryRecordRow.target_memory_id.is_not(None),
                references_any(session, MemoryRecordRow.evidence_source_ids, sources),
            )
        )
        if sources
        else ()
    ):
        candidate.status = "superseded"
        for version in session.scalars(
            select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.memory_id == candidate.memory_id,
            )
        ):
            version.content = ""
    session.flush()


def invalidate_sources_in_session(
    session: Session, actor: MemoryActor, owner: MemoryOwnerRow, source_ids: set[str]
) -> None:
    """Hide original sources and suspend all dependent bodies in the same transaction."""
    rows = tuple(
        session.scalars(
            select(MemorySourceRow).where(
                *owner_filter(MemorySourceRow, actor), MemorySourceRow.source_id.in_(source_ids)
            )
        )
    )
    if len(rows) != len(source_ids):
        raise MemoryNotFound("source is absent")
    owner.privacy_epoch += 1
    owner.memory_revision += 1
    for row in rows:
        row.visible = False
        row.permit_revoked = True
        row.reuse_blocked = True
    clear_derivatives_in_session(session, owner, source_ids)
    for row in session.scalars(
        select(MemoryRecordRow).where(
            *owner_filter(MemoryRecordRow, actor),
            references_any(session, MemoryRecordRow.evidence_source_ids, source_ids),
        )
    ):
        enqueue_index_in_session(session, owner, row, delete=True)
        row.content = ""
        if row.is_current:
            row.status = "invalidated" if row.status == "active" else "superseded"
    rebuild_digest_in_session(session, owner)


def revoke_turn_sources_in_session(session: Session, actor: MemoryActor, turn_id: str) -> None:
    """Explicit revocation cancels pending duties without deleting existing facts."""
    from financeclaw.shared.memory.repository import lock_owner

    lock_owner(session, actor)
    for row in session.scalars(
        select(MemorySourceRow).where(
            *owner_filter(MemorySourceRow, actor), MemorySourceRow.turn_id == turn_id
        )
    ):
        row.permit_revoked = True


class MemoryLifecycle:
    """Expose authenticated source hiding with the required business-before-owner lock order."""

    def __init__(self, sessions):
        """Reuse the application transaction factory without an alternate source authority."""
        self.sessions = sessions

    def hide_source(
        self,
        actor: MemoryActor,
        source_id: str,
        *,
        mutation_id: str,
        expected_source_version: int | None = None,
    ) -> dict:
        """Hide one exact source and invalidate all its dependent memory in one transaction."""
        with self.sessions.begin() as session:
            return self.hide_source_in_session(
                session,
                actor,
                source_id,
                mutation_id=mutation_id,
                expected_source_version=expected_source_version,
            )

    def hide_source_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        source_id: str,
        *,
        mutation_id: str,
        expected_source_version: int | None = None,
    ) -> dict:
        """Lock real conversation/Turn before owner; interaction responses remain business facts."""
        from financeclaw.shared.audit.models import AuditEventType, AuditRecord
        from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
        from financeclaw.shared.memory.authorization import require_scope
        from financeclaw.shared.memory.models import MemoryConflict, MemoryPermissionError
        from financeclaw.shared.memory.mutations import MemoryMutationService, receipt_id
        from financeclaw.shared.memory.repository import canonical_hash, lock_owner
        from financeclaw.shared.turns.tables import ConversationTurnRow

        require_scope(actor, "memory:delete")
        if actor.kind != "user":
            raise MemoryPermissionError("only an authenticated user may hide original sources")
        source = session.scalars(
            select(MemorySourceRow).where(
                *owner_filter(MemorySourceRow, actor), MemorySourceRow.source_id == source_id
            )
        ).one_or_none()
        if source is None:
            raise MemoryNotFound("source is absent")
        if source.conversation_id:
            session.scalars(
                select(ConversationRow)
                .where(
                    *owner_filter(ConversationRow, actor),
                    ConversationRow.conversation_id == source.conversation_id,
                )
                .with_for_update()
            ).one()
        if source.turn_id:
            session.scalars(
                select(ConversationTurnRow)
                .where(
                    *owner_filter(ConversationTurnRow, actor),
                    ConversationTurnRow.turn_id == source.turn_id,
                )
                .with_for_update()
            ).one()
        message = None
        if source.source_kind in {"user_message", "assistant_message"}:
            message = session.scalars(
                select(ConversationMessageRow)
                .where(
                    ConversationMessageRow.message_id == source.object_id,
                    ConversationMessageRow.turn_id == source.turn_id,
                    ConversationMessageRow.conversation_id == source.conversation_id,
                )
                .with_for_update()
            ).one_or_none()
        owner = lock_owner(session, actor)
        service = MemoryMutationService(self.sessions)
        payload_hash = canonical_hash(
            {
                "operation": "hide_source",
                "source_id": source_id,
                "expected_source_version": expected_source_version,
            }
        )
        replay = service._replay(session, actor, mutation_id, payload_hash)
        if replay:
            return replay
        if expected_source_version is not None and source.source_version != expected_source_version:
            raise MemoryConflict("source version changed")
        if message is not None:
            message.visible = False
        invalidate_sources_in_session(session, actor, owner, {source_id})
        result = {
            "status": "source_hidden",
            "source_id": source_id,
            "memory_revision": owner.memory_revision,
            "privacy_epoch": owner.privacy_epoch,
        }
        service.audit.append_in_session(
            session,
            AuditRecord(
                audit_id=receipt_id(actor, mutation_id),
                event_type=AuditEventType.MEMORY_SOURCE_INVALIDATED,
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                conversation_id=source.conversation_id,
                turn_id=source.turn_id,
                resource_type="memory_source",
                resource_id=source_id,
                resource_version=str(source.source_version),
                action="memory.source_hidden",
                decision="hidden",
                policy_version="memory/1",
                payload_hash=payload_hash,
                evidence_refs=(source_id,),
                metadata={"result": result},
            ),
        )
        return result


def hide_message(sessions, actor: MemoryActor, message_id: str, *, mutation_id: str) -> dict:
    """Resolve a registered Journal source without exposing another owner's original body."""
    with sessions.begin() as session:
        source_id = session.scalar(
            select(MemorySourceRow.source_id).where(
                *owner_filter(MemorySourceRow, actor),
                MemorySourceRow.object_id == message_id,
                MemorySourceRow.source_kind.in_(["user_message", "assistant_message"]),
            )
        )
        if source_id is None:
            raise MemoryNotFound("message source is absent")
        return MemoryLifecycle(sessions).hide_source_in_session(
            session, actor, source_id, mutation_id=mutation_id
        )
