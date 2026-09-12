"""Read exact trusted business sources; generated summaries never become user evidence."""

import json
from dataclasses import dataclass

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.memory.models import (
    EvidenceRef,
    MemoryActor,
    MemoryNotFound,
    MemoryPermissionError,
)
from financeclaw.shared.memory.repository import content_hash, owner_filter
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow, MemorySourceRow
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow


@dataclass(frozen=True)
class EvidenceDocument:
    """An exact original source with a server-verified role and immutable identity."""

    ref: EvidenceRef
    content: str
    source_kind: str


def interaction_content(row: InteractionRow) -> str:
    """Preserve accepted answer semantics together with its bound question schema."""
    return json.dumps(
        {"request": row.request, "response": row.response},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class EvidenceReader:
    """Resolve bodies through their business authority and validate hash, owner and span."""

    def read_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        refs: tuple[EvidenceRef, ...],
        *,
        for_derivation: bool = False,
    ) -> tuple[EvidenceDocument, ...]:
        """Read no more than the bounded supplied references; never scan conversations."""
        if len(refs) > 128 or len({ref.source_id for ref in refs}) != len(refs):
            raise MemoryPermissionError("evidence references must be bounded and unique")
        sources = {
            source.source_id: source
            for source in session.scalars(
                select(MemorySourceRow).where(
                    *owner_filter(MemorySourceRow, actor),
                    MemorySourceRow.source_id.in_([ref.source_id for ref in refs]),
                )
            )
        }
        owner = (
            session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
            if for_derivation
            else None
        )
        bodies = self._bodies(session, actor, tuple(sources.values()))
        documents = []
        for ref in refs:
            source = sources.get(ref.source_id)
            if source is None or not source.visible or not source.version_valid:
                raise MemoryNotFound("source is unavailable")
            if (
                source.source_kind,
                source.source_version,
                source.content_hash,
                source.source_seq,
            ) != (ref.source_kind, ref.source_version, ref.content_hash, ref.source_seq):
                raise MemoryPermissionError("source version or hash mismatch")
            if for_derivation:
                from financeclaw.shared.memory.authorization import validate_source_permit

                validate_source_permit(session, actor, source, owner=owner)
            if source.source_id not in bodies:
                raise MemoryNotFound("original source is unavailable")
            content = bodies[source.source_id]
            if for_derivation:
                from financeclaw.shared.memory.policies import reject_secrets

                reject_secrets(content)
            if content_hash(content) != ref.content_hash:
                raise MemoryPermissionError("original source has changed")
            if ref.span is not None and not (0 <= ref.span[0] < ref.span[1] <= len(content)):
                raise MemoryPermissionError("invalid original source span")
            documents.append(EvidenceDocument(ref, content, source.source_kind))
        return tuple(documents)

    def _bodies(
        self, session: Session, actor: MemoryActor, sources: tuple[MemorySourceRow, ...]
    ) -> dict[str, str]:
        """Batch each business source kind while applying owner filters before body access."""
        bodies = {}
        messages = {
            source.object_id: source
            for source in sources
            if source.source_kind in {"user_message", "assistant_message"}
        }
        if messages:
            rows = session.scalars(
                select(ConversationMessageRow)
                .join(
                    ConversationRow,
                    ConversationRow.conversation_id == ConversationMessageRow.conversation_id,
                )
                .where(
                    *owner_filter(ConversationRow, actor),
                    ConversationMessageRow.message_id.in_(messages),
                    ConversationMessageRow.visible.is_(True),
                )
            )
            for row in rows:
                source = messages[row.message_id]
                role = "user" if source.source_kind == "user_message" else "assistant"
                if row.role == role and row.turn_id == source.turn_id:
                    bodies[source.source_id] = row.content
        interactions = {
            source.object_id: source
            for source in sources
            if source.source_kind == "interaction_answer"
        }
        if interactions:
            rows = session.scalars(
                select(InteractionRow)
                .join(ConversationTurnRow, ConversationTurnRow.turn_id == InteractionRow.turn_id)
                .where(
                    *owner_filter(ConversationTurnRow, actor),
                    InteractionRow.interaction_id.in_(interactions),
                )
            )
            for row in rows:
                if (
                    row.response is not None
                    and row.status in {"resolved", "rejected"}
                    and row.decided_by == actor.subject_id
                ):
                    bodies[interactions[row.interaction_id].source_id] = interaction_content(row)
        actions = {}
        for source in sources:
            if source.source_kind == "memory_action":
                memory_id, revision = source.object_id.rsplit(":", 1)
                actions[(memory_id, int(revision))] = source
            elif source.source_kind not in {
                "user_message",
                "assistant_message",
                "interaction_answer",
            }:
                raise MemoryPermissionError("unregistered evidence source kind")
        if actions:
            rows = session.scalars(
                select(MemoryRecordRow).where(
                    *owner_filter(MemoryRecordRow, actor),
                    tuple_(MemoryRecordRow.memory_id, MemoryRecordRow.revision).in_(actions),
                )
            )
            for row in rows:
                if row.content:
                    bodies[actions[(row.memory_id, row.revision)].source_id] = row.content
        return bodies
