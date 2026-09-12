"""Closed-source partition planning and atomically committed extraction outputs."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from financeclaw.memory_worker.model import validate_model_sources
from financeclaw.memory_worker.prompts import (
    EXTRACTION_PROMPT,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    MemorySuggestions,
)
from financeclaw.memory_worker.runner import UnsupportedPipeline
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.models import (
    EvidenceRef,
    MemoryActor,
    MemoryNotFound,
    MemoryPermissionError,
)
from financeclaw.shared.memory.repository import canonical_hash, lock_owner
from financeclaw.shared.memory.scheduling import ensure_consolidation_in_session
from financeclaw.shared.memory.tables import MemoryExtractionRow
from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow

EXTRACTION_DESTINATION = "memory_extract"


def worker_actor(event: OutboxEvent, refs=()) -> MemoryActor:
    """Only persisted owner and exact source references can supply worker identity."""
    return MemoryActor(
        tenant_id=event.tenant_id,
        subject_id=event.subject_id,
        kind="worker",
        scopes=frozenset({"memory:read", "memory:write"}),
        turn_id=event.payload.get("turn_id"),
        conversation_id=event.payload.get("conversation_id"),
        permit_source_ids=tuple(ref.source_id for ref in refs),
    )


def input_documents(documents) -> list[dict]:
    """Carry full original text and immutable references, not assistant-rephrased evidence."""
    return [
        {"ref": item.ref.model_dump(mode="json"), "content": item.content} for item in documents
    ]


class ExtractionHandler:
    """Prepare is model-free; all parts must close before any can be consolidated."""

    def __init__(
        self,
        sessions,
        outbox,
        model,
        *,
        consolidation_model_version,
        max_parts=8,
        max_sources=16,
        enabled=True,
    ):
        """Bound work and inject the same SQL session factory into every transaction."""
        self.sessions = sessions
        self.outbox = outbox
        self.model = model
        self.consolidation_model_version = consolidation_model_version
        self.max_parts = max_parts
        self.max_sources = max_sources
        self.evidence = EvidenceReader()
        self.enabled = enabled

    async def process(self, event: OutboxEvent) -> None:
        """Validate frozen contract before choosing prepare or one extraction part."""
        if event.destination != EXTRACTION_DESTINATION:
            raise ValueError("wrong extraction destination")
        if not self.enabled:
            await asyncio.to_thread(self._skip, event, worker_actor(event), "deployment_disabled")
            return
        if event.payload.get("pipeline_version") != PIPELINE_VERSION:
            raise UnsupportedPipeline("unsupported memory extraction pipeline")
        frozen = event.payload.get("model_profile_version")
        if frozen != self.model.fingerprint:
            raise UnsupportedPipeline("frozen memory extraction model differs")
        refs = tuple(EvidenceRef.model_validate(ref) for ref in event.payload["sources"])
        actor = worker_actor(event, refs)
        try:
            documents = await asyncio.to_thread(self._read, event, actor, refs)
        except (MemoryPermissionError, LookupError):
            await asyncio.to_thread(self._skip, event, actor, "source_ineligible")
            return
        if event.event_type == "memory.extract.prepare":
            try:
                await asyncio.to_thread(self._prepare, event, actor, documents)
            except (MemoryPermissionError, MemoryNotFound):
                await asyncio.to_thread(self._skip, event, actor, "source_ineligible")
            return
        if event.event_type != "memory.extract.part":
            raise UnsupportedPipeline("unsupported memory extraction event")
        await asyncio.to_thread(self._validate_part, event)
        payload = {
            "documents": input_documents(documents),
            "conversation_id": actor.conversation_id,
        }
        try:
            await asyncio.to_thread(self._model_preflight, event, actor, refs, payload)
        except MemoryPermissionError:
            await asyncio.to_thread(self._skip, event, actor, "model_policy_denied")
            return
        output = await self.model.generate(
            event,
            EXTRACTION_PROMPT,
            payload,
            snapshot_id=event.payload["closure_hash"],
            max_attempts=2,
        )
        try:
            await asyncio.to_thread(self._commit, event, actor, refs, output)
        except (MemoryPermissionError, MemoryNotFound):
            await asyncio.to_thread(self._skip, event, actor, "source_ineligible")

    def _validate_part(self, event):
        """Verify immutable part identity against its durable model-free preparation manifest."""
        with self.sessions() as session:
            parent = session.get(OutboxEventRow, event.payload["prepare_event_id"])
            if parent is None or (parent.tenant_id, parent.subject_id) != (
                event.tenant_id,
                event.subject_id,
            ):
                raise ValueError("extraction prepare event is absent or foreign")
            metadata = parent.processing_metadata or {}
            manifest = metadata.get("manifest", [])
            index = event.payload["part_index"]
            if (
                parent.status != "published"
                or metadata.get("closure_hash") != event.payload["closure_hash"]
                or len(manifest) != event.payload["part_count"]
                or not 0 <= index < len(manifest)
                or manifest[index] != event.payload["sources"]
            ):
                raise ValueError("extraction part does not match its immutable manifest")

    def _read(self, event, actor, refs):
        """Read a bounded source snapshot in a short transaction before any model I/O."""
        with self.sessions.begin() as session:
            self._validate_completion(session, event)
            validate_model_sources(session, actor, refs, self.model.profile)
            return self.evidence.read_in_session(session, actor, refs, for_derivation=True)

    def _model_preflight(self, event, actor, refs, payload):
        """Revalidate permit capacity immediately before reserving a paid model attempt."""
        tokens = self.model.estimate(EXTRACTION_PROMPT, payload)
        with self.sessions.begin() as session:
            self._validate_completion(session, event)
            self.evidence.read_in_session(session, actor, refs, for_derivation=True)
            validate_model_sources(session, actor, refs, self.model.profile, input_tokens=tokens)

    def _validate_completion(self, session, event):
        """Recheck the exact final Journal and completed Turn without copying assistant text."""
        turn = session.scalar(
            select(ConversationTurnRow).where(
                ConversationTurnRow.tenant_id == event.tenant_id,
                ConversationTurnRow.subject_id == event.subject_id,
                ConversationTurnRow.turn_id == event.payload.get("turn_id"),
                ConversationTurnRow.conversation_id == event.payload.get("conversation_id"),
            )
        )
        if turn is None or turn.status != "completed":
            raise MemoryPermissionError("memory extraction requires a completed Turn")
        final = session.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.turn_id == turn.turn_id,
                ConversationMessageRow.message_id == event.payload.get("final_message_id"),
                ConversationMessageRow.role == "assistant",
                ConversationMessageRow.parent_message_id.is_(None),
                ConversationMessageRow.visible.is_(True),
            )
        )
        if final is None or final.content_hash != event.payload.get("final_message_hash"):
            raise MemoryPermissionError("completed task context is unavailable or changed")

    def _prepare(self, event, actor, documents):
        """Freeze complete-source bins and their child events in one transaction."""
        ordered = sorted(documents, key=lambda item: item.ref.source_seq)
        parts, current = [], []
        reason = None
        for item in ordered:
            proposed = [*current, item]
            payload = {
                "documents": input_documents(proposed),
                "conversation_id": actor.conversation_id,
            }
            if (
                len(proposed) > self.max_sources
                or self.model.estimate(EXTRACTION_PROMPT, payload) > self.model.planner.input_limit
            ):
                if current:
                    parts.append(current)
                    current = []
                single = {
                    "documents": input_documents([item]),
                    "conversation_id": actor.conversation_id,
                }
                if self.model.estimate(EXTRACTION_PROMPT, single) > self.model.planner.input_limit:
                    reason = "source_oversize"
                    break
            current.append(item)
        if current:
            parts.append(current)
        if len(parts) > self.max_parts:
            reason = "partition_limit"
        if reason or not parts:
            self._skip(event, actor, reason or "no_output")
            return
        manifest = [[item.ref.model_dump(mode="json") for item in part] for part in parts]
        closure_hash = canonical_hash(
            {
                "prepare": event.event_id,
                "parts": manifest,
                "pipeline": PIPELINE_VERSION,
                "model": self.model.fingerprint,
                "schema": SCHEMA_VERSION,
            }
        )
        with self.sessions.begin() as session:
            lock_owner(session, actor)
            self._validate_completion(session, event)
            self.evidence.read_in_session(
                session, actor, tuple(item.ref for item in documents), for_derivation=True
            )
            for index, refs in enumerate(manifest):
                self.outbox.enqueue_in_session(
                    session,
                    OutboxEvent(
                        event_id=f"memory-part-{closure_hash}-{index}",
                        event_type="memory.extract.part",
                        destination=EXTRACTION_DESTINATION,
                        aggregate_type="memory_owner",
                        aggregate_id=event.aggregate_id,
                        tenant_id=event.tenant_id,
                        subject_id=event.subject_id,
                        payload={
                            "sources": refs,
                            "turn_id": actor.turn_id,
                            "conversation_id": actor.conversation_id,
                            "final_message_id": event.payload["final_message_id"],
                            "final_message_hash": event.payload["final_message_hash"],
                            "prepare_event_id": event.event_id,
                            "closure_hash": closure_hash,
                            "part_index": index,
                            "part_count": len(parts),
                            "pipeline_version": PIPELINE_VERSION,
                            "model_profile_version": self.model.fingerprint,
                            "schema_version": SCHEMA_VERSION,
                        },
                    ),
                )
            self.outbox.complete_in_session(
                session,
                event.event_id,
                event.claim_epoch,
                outcome="prepared",
                metadata={
                    "manifest": manifest,
                    "closure_hash": closure_hash,
                    "retain_for_children": True,
                },
            )

    def _commit(self, event, actor, refs, output: MemorySuggestions):
        """Validate model citations and permission again, then close only a complete cohort."""
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            self._validate_completion(session, event)
            validate_model_sources(session, actor, refs, self.model.profile)
            documents = self.evidence.read_in_session(session, actor, refs, for_derivation=True)
            sources = {item.ref.source_id: item for item in documents}
            for suggestion in output.suggestions:
                for citation in suggestion.evidence:
                    document = sources.get(citation.source_id)
                    if document is None:
                        raise ValueError("model citation is outside the claimed source set")
                    if citation.span is not None:
                        start, end = citation.span
                        if not 0 <= start < end <= len(document.content):
                            raise ValueError("model citation span is outside original source")
            payload = event.payload
            extraction_id = f"extraction-{payload['closure_hash']}-{payload['part_index']}"
            if session.get(MemoryExtractionRow, extraction_id) is None:
                session.add(
                    MemoryExtractionRow(
                        extraction_id=extraction_id,
                        tenant_id=event.tenant_id,
                        subject_id=event.subject_id,
                        closure_hash=payload["closure_hash"],
                        prepare_event_id=payload["prepare_event_id"],
                        part_index=payload["part_index"],
                        part_count=payload["part_count"],
                        pipeline_version=PIPELINE_VERSION,
                        model_profile_version=self.model.fingerprint,
                        schema_version=SCHEMA_VERSION,
                        evidence_source_ids=[ref.source_id for ref in refs],
                        evidence=[ref.model_dump(mode="json") for ref in refs],
                        output=output.model_dump(mode="json"),
                        coverage={"complete": True},
                    )
                )
                session.flush()
            cohort = list(
                session.scalars(
                    select(MemoryExtractionRow).where(
                        MemoryExtractionRow.tenant_id == event.tenant_id,
                        MemoryExtractionRow.subject_id == event.subject_id,
                        MemoryExtractionRow.closure_hash == payload["closure_hash"],
                        MemoryExtractionRow.pipeline_version == PIPELINE_VERSION,
                    )
                )
            )
            complete = len(cohort) == payload["part_count"] and {
                item.part_index for item in cohort
            } == set(range(payload["part_count"]))
            if any(item.disposition == "quarantined" for item in cohort):
                self._quarantine_cohort(session, event, "incomplete_closure")
            if complete and all(
                item.coverage.get("complete") and item.disposition == "pending" for item in cohort
            ):
                if all(item.extraction_revision is None for item in cohort):
                    owner.extraction_revision += 1
                    for item in cohort:
                        item.extraction_revision = owner.extraction_revision
                    session.flush()
                    ensure_consolidation_in_session(
                        session, owner, model_profile_version=self.consolidation_model_version
                    )
            self.outbox.complete_in_session(
                session,
                event.event_id,
                event.claim_epoch,
                outcome="extracted" if output.suggestions else "no_output",
                metadata={"extraction_id": extraction_id, "closure_ready": complete},
            )

    def _skip(self, event, actor, reason):
        """Complete revoked or oversized inputs without creating a partial task."""
        with self.sessions.begin() as session:
            lock_owner(session, actor)
            if event.payload.get("closure_hash"):
                self._quarantine_cohort(session, event, reason)
            self.outbox.complete_in_session(
                session, event.event_id, event.claim_epoch, outcome=reason
            )

    def _quarantine_cohort(self, session, event, reason):
        """Invalidate mixed outputs rather than silently consuming only successful fragments."""
        payload = event.payload
        if "part_index" in payload:
            extraction_id = f"extraction-{payload['closure_hash']}-{payload['part_index']}"
            if session.get(MemoryExtractionRow, extraction_id) is None:
                session.add(
                    MemoryExtractionRow(
                        extraction_id=extraction_id,
                        tenant_id=event.tenant_id,
                        subject_id=event.subject_id,
                        closure_hash=payload["closure_hash"],
                        prepare_event_id=payload["prepare_event_id"],
                        part_index=payload["part_index"],
                        part_count=payload["part_count"],
                        pipeline_version=payload["pipeline_version"],
                        model_profile_version=payload["model_profile_version"],
                        schema_version=payload.get("schema_version", SCHEMA_VERSION),
                        evidence_source_ids=[ref["source_id"] for ref in payload["sources"]],
                        evidence=payload["sources"],
                        output={},
                        coverage={"complete": False},
                        disposition="quarantined",
                        disposition_reason=reason,
                    )
                )
                session.flush()
        for row in session.scalars(
            select(MemoryExtractionRow).where(
                MemoryExtractionRow.tenant_id == event.tenant_id,
                MemoryExtractionRow.subject_id == event.subject_id,
                MemoryExtractionRow.closure_hash == event.payload.get("closure_hash"),
                MemoryExtractionRow.disposition == "pending",
            )
        ):
            row.disposition = "quarantined"
            row.disposition_reason = reason
            row.output = {}
            row.consumed_at = datetime.now(UTC)

    def fail(self, event, reason, *, terminal=False):
        """On dead letter quarantine the exact incomplete closure, preserving other owners."""
        with self.sessions.begin() as session:
            lock_owner(session, worker_actor(event))
            row = self.outbox.require_claim_in_session(session, event.event_id, event.claim_epoch)
            dead = terminal or row.attempts + 1 >= 8
            if dead and event.payload.get("closure_hash"):
                self._quarantine_cohort(session, event, reason)
            self.outbox.fail_in_session(
                session,
                event.event_id,
                event.claim_epoch,
                error=reason,
                max_attempts=8,
                terminal=terminal,
            )
