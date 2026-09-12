"""Owner-scoped, version-fenced consolidation with exact input disposition."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from financeclaw.memory_worker.extraction import input_documents, worker_actor
from financeclaw.memory_worker.model import validate_model_sources
from financeclaw.memory_worker.prompts import (
    CONSOLIDATION_PROMPT,
    PIPELINE_VERSION,
    MemorySuggestions,
)
from financeclaw.memory_worker.runner import UnsupportedPipeline
from financeclaw.shared.conversation.tables import ConversationRow
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.models import (
    EvidenceRef,
    MemoryConflict,
    MemoryMutation,
    MemoryPermissionError,
    utc,
)
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import canonical_hash, lock_owner, owner_filter
from financeclaw.shared.memory.scheduling import (
    advance_consolidated_revision_in_session,
    ensure_consolidation_in_session,
)
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryRecordRow, MemorySourceRow

CONSOLIDATION_DESTINATION = "memory_consolidate"


@dataclass(frozen=True)
class ConsolidationSnapshot:
    """An immutable finite input set; its versions are checked again at commit."""

    memory_revision: int
    policy_revision: int
    privacy_epoch: int
    extraction_ids: tuple[str, ...]
    refs: tuple[EvidenceRef, ...]
    payload: dict
    fingerprint: str


class ConsolidationHandler:
    """Release DB locks before generation and refuse every stale write proposal."""

    def __init__(
        self,
        sessions,
        outbox,
        model,
        *,
        enabled=True,
        candidate_seconds=604800,
        auto_commit_low_risk=True,
    ):
        """Reuse domain evidence and mutations instead of giving the model SQL authority."""
        self.sessions = sessions
        self.outbox = outbox
        self.model = model
        self.evidence = EvidenceReader()
        self.mutations = MemoryMutationService(
            sessions, candidate_seconds=candidate_seconds, auto_commit_low_risk=auto_commit_low_risk
        )
        self.enabled = enabled

    async def process(self, event):
        """Use at most three snapshots and four persisted calls across all retries."""
        if (
            event.destination != CONSOLIDATION_DESTINATION
            or event.payload.get("pipeline_version") != PIPELINE_VERSION
        ):
            raise UnsupportedPipeline("unsupported memory consolidation pipeline")
        frozen = event.payload.get("model_profile_version")
        if self.enabled and frozen != self.model.fingerprint:
            raise UnsupportedPipeline("frozen memory consolidation model differs")
        for _ in range(3):
            snapshot = await asyncio.to_thread(self._snapshot, event)
            if not snapshot.extraction_ids:
                await asyncio.to_thread(self._commit, event, snapshot, MemorySuggestions())
                return
            output = MemorySuggestions()
            if any(group["suggestions"] for group in snapshot.payload["groups"]):
                try:
                    await asyncio.to_thread(self._model_preflight, event, snapshot)
                except (MemoryPermissionError, MemoryConflict):
                    await asyncio.to_thread(
                        self.outbox.record_version_conflict,
                        event.event_id,
                        claim_epoch=event.claim_epoch,
                    )
                    continue
                output = await self.model.generate(
                    event,
                    CONSOLIDATION_PROMPT,
                    snapshot.payload,
                    snapshot_id=snapshot.fingerprint,
                    max_attempts=4,
                )
            try:
                await asyncio.to_thread(self._commit, event, snapshot, output)
                return
            except (MemoryConflict, MemoryPermissionError):
                await asyncio.to_thread(
                    self.outbox.record_version_conflict,
                    event.event_id,
                    claim_epoch=event.claim_epoch,
                )
                continue
        raise MemoryConflict("consolidation snapshot changed repeatedly")

    def _snapshot(self, event):
        """Read consecutive ready groups, quarantine ineligible ones, and persist exact IDs."""
        actor = worker_actor(event)
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            if owner.active_consolidation_event_id != event.event_id:
                raise LookupError("consolidation event no longer owns the wakeup")
            self.outbox.require_claim_in_session(session, event.event_id, event.claim_epoch)
            rows = list(
                session.scalars(
                    select(MemoryExtractionRow)
                    .where(
                        *owner_filter(MemoryExtractionRow, actor),
                        MemoryExtractionRow.disposition == "pending",
                        MemoryExtractionRow.extraction_revision.is_not(None),
                    )
                    .order_by(
                        MemoryExtractionRow.extraction_revision, MemoryExtractionRow.part_index
                    )
                    .limit(256)
                )
            )
            grouped = {}
            for row in rows:
                grouped.setdefault(row.extraction_revision, []).append(row)
            current = list(
                session.scalars(
                    select(MemoryRecordRow)
                    .where(
                        *owner_filter(MemoryRecordRow, actor),
                        MemoryRecordRow.is_current.is_(True),
                        MemoryRecordRow.status == "active",
                    )
                    .order_by(MemoryRecordRow.owner_revision.desc())
                    .limit(64)
                )
            )
            valid_current = []
            for row in current:
                if not owner.read_enabled:
                    break
                if row.expires_at and utc(row.expires_at) <= datetime.now(UTC):
                    continue
                try:
                    validate_model_sources(
                        session,
                        actor,
                        tuple(EvidenceRef.model_validate(ref) for ref in row.evidence),
                        self.model.profile,
                    )
                    self.evidence.read_in_session(
                        session,
                        actor,
                        tuple(EvidenceRef.model_validate(ref) for ref in row.evidence),
                    )
                except (MemoryPermissionError, LookupError, ValueError):
                    continue
                valid_current.append(row)
            current_values = [
                {
                    key: getattr(row, key)
                    for key in (
                        "memory_id",
                        "revision",
                        "kind",
                        "field",
                        "scope_type",
                        "scope_id",
                        "content",
                        "source_watermark",
                    )
                }
                for row in valid_current
            ]
            payload = {"current": current_values, "groups": []}
            selected, all_refs = [], {}
            for group in list(grouped.values())[:32]:
                if not self.enabled:
                    self._dispose(group, "quarantined", "deployment_disabled", clear=True)
                    continue
                if len(group) != group[0].part_count or {part.part_index for part in group} != set(
                    range(group[0].part_count)
                ):
                    self._dispose(group, "quarantined", "incomplete_ready_group", clear=True)
                    continue
                refs = tuple(
                    EvidenceRef.model_validate(value) for part in group for value in part.evidence
                )
                if len(set(all_refs) | {ref.source_id for ref in refs}) > 128:
                    break
                candidate_actor = actor.model_copy(
                    update={"permit_source_ids": tuple(ref.source_id for ref in refs)}
                )
                try:
                    validate_model_sources(session, candidate_actor, refs, self.model.profile)
                    documents = self.evidence.read_in_session(
                        session, candidate_actor, refs, for_derivation=True
                    )
                except (MemoryPermissionError, LookupError):
                    self._dispose(group, "quarantined", "source_ineligible", clear=True)
                    continue
                value = {
                    "extraction_ids": [part.extraction_id for part in group],
                    "suggestions": [
                        suggestion
                        for part in group
                        for suggestion in part.output.get("suggestions", [])
                    ],
                    "documents": input_documents(documents),
                }
                proposed = {**payload, "groups": [*payload["groups"], value]}
                if (
                    self.model.estimate(CONSOLIDATION_PROMPT, proposed)
                    > self.model.planner.input_limit
                ):
                    if selected:
                        break
                    self._dispose(group, "quarantined", "capacity_exceeded", clear=False)
                    continue
                payload = proposed
                selected.extend(group)
                all_refs.update((ref.source_id, ref) for ref in refs)
            ids = tuple(row.extraction_id for row in selected)
            fingerprint = canonical_hash(
                {
                    "ids": ids,
                    "memory_revision": owner.memory_revision,
                    "policy_revision": owner.policy_revision,
                    "privacy_epoch": owner.privacy_epoch,
                    "payload": payload,
                }
            )
            self.outbox.update_metadata_in_session(
                session,
                event.event_id,
                event.claim_epoch,
                {
                    "snapshot_ids": list(ids),
                    "snapshot_fingerprint": fingerprint,
                    "model_profile_version": self.model.fingerprint,
                },
            )
            advance_consolidated_revision_in_session(session, owner)
            return ConsolidationSnapshot(
                owner.memory_revision,
                owner.policy_revision,
                owner.privacy_epoch,
                ids,
                tuple(all_refs.values()),
                payload,
                fingerprint,
            )

    def _commit(self, event, snapshot, output):
        """Compare the entire snapshot and atomically consume inputs, facts and wakeup."""
        actor = worker_actor(event, snapshot.refs)
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            if owner.active_consolidation_event_id != event.event_id:
                raise LookupError("consolidation event no longer owns the wakeup")
            if (owner.memory_revision, owner.policy_revision, owner.privacy_epoch) != (
                snapshot.memory_revision,
                snapshot.policy_revision,
                snapshot.privacy_epoch,
            ):
                raise MemoryConflict("memory owner changed during generation")
            documents = self.evidence.read_in_session(
                session, actor, snapshot.refs, for_derivation=True
            )
            validate_model_sources(session, actor, snapshot.refs, self.model.profile)
            by_source = {document.ref.source_id: document for document in documents}
            parts = (
                list(
                    session.scalars(
                        select(MemoryExtractionRow).where(
                            *owner_filter(MemoryExtractionRow, actor),
                            MemoryExtractionRow.extraction_id.in_(snapshot.extraction_ids),
                        )
                    )
                )
                if snapshot.extraction_ids
                else []
            )
            if len(parts) != len(snapshot.extraction_ids) or any(
                part.disposition != "pending" for part in parts
            ):
                raise MemoryConflict("consolidation inputs changed during generation")
            for index, suggestion in enumerate(output.suggestions):
                refs = []
                for citation in suggestion.evidence:
                    document = by_source.get(citation.source_id)
                    if document is None:
                        raise ValueError("consolidation introduced unsupported source")
                    refs.append(document.ref.model_copy(update={"span": citation.span}))
                current = self._target(session, actor, suggestion)
                if current is not None and (
                    current.content == suggestion.content
                    or current.source_watermark > max(ref.source_seq for ref in refs)
                ):
                    continue
                mutation = MemoryMutation(
                    mutation_id=f"consolidate:{event.event_id}:{snapshot.fingerprint}:{index}",
                    operation="update" if current is not None else "create",
                    memory_id=current.memory_id if current is not None else None,
                    expected_revision=current.revision if current is not None else None,
                    kind=suggestion.kind,
                    scope_type=suggestion.scope_type,
                    scope_id=suggestion.scope_id,
                    field=suggestion.field,
                    content=suggestion.content,
                    evidence=tuple(refs),
                )
                mutation_actor = self._mutation_actor(session, actor, mutation)
                self.mutations.apply_in_session(session, mutation_actor, mutation)
            self._dispose(parts, "consumed", "consolidated", clear=False)
            self._finish(
                session, event, owner, outcome="consolidated" if output.suggestions else "no_output"
            )

    def _model_preflight(self, event, snapshot):
        """Validate current source permission and total request capacity before model I/O."""
        actor = worker_actor(event, snapshot.refs)
        tokens = self.model.estimate(CONSOLIDATION_PROMPT, snapshot.payload)
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            if (owner.memory_revision, owner.policy_revision, owner.privacy_epoch) != (
                snapshot.memory_revision,
                snapshot.policy_revision,
                snapshot.privacy_epoch,
            ):
                raise MemoryConflict("memory owner changed before model request")
            self.evidence.read_in_session(session, actor, snapshot.refs, for_derivation=True)
            validate_model_sources(
                session,
                actor,
                snapshot.refs,
                self.model.profile,
                input_tokens=tokens,
                phase="consolidation",
            )

    def _target(self, session, actor, suggestion):
        """Only current profile keys are implicit update targets; task IDs are never guessed."""
        if suggestion.kind != "profile":
            return None
        return session.scalar(
            select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.is_current.is_(True),
                MemoryRecordRow.status == "active",
                MemoryRecordRow.kind == "profile",
                MemoryRecordRow.field == suggestion.field.value,
                MemoryRecordRow.scope_type == suggestion.scope_type,
                MemoryRecordRow.scope_id == suggestion.scope_id,
            )
        )

    def _mutation_actor(self, session, actor, mutation):
        """Bind scoped proposals to their actual source conversations and registered agents."""
        sources = list(
            session.scalars(
                select(MemorySourceRow).where(
                    *owner_filter(MemorySourceRow, actor),
                    MemorySourceRow.source_id.in_([ref.source_id for ref in mutation.evidence]),
                )
            )
        )
        conversations = {source.conversation_id for source in sources if source.conversation_id}
        conversation_id = next(iter(conversations)) if len(conversations) == 1 else None
        agents = (
            set(
                session.scalars(
                    select(ConversationRow.agent_id).where(
                        *owner_filter(ConversationRow, actor),
                        ConversationRow.conversation_id.in_(conversations),
                    )
                )
            )
            if conversations
            else set()
        )
        agent_id = next(iter(agents)) if len(agents) == 1 else None
        if mutation.kind == "task" and mutation.scope_type == "user":
            raise MemoryPermissionError("automatic task memory needs explicit source scope")
        return actor.model_copy(update={"conversation_id": conversation_id, "agent_id": agent_id})

    def _dispose(self, parts, disposition, reason, *, clear):
        """Record exactly which ready outputs were consumed or explicitly quarantined."""
        for row in parts:
            row.disposition = disposition
            row.disposition_reason = reason
            row.consumed_at = datetime.now(UTC)
            if clear:
                row.output = {}

    def _finish(self, session, event, owner, *, outcome):
        """Owner lock joins old completion, pointer clearing, and any successor creation."""
        self.outbox.complete_in_session(session, event.event_id, event.claim_epoch, outcome=outcome)
        owner.active_consolidation_event_id = None
        session.flush()
        advance_consolidated_revision_in_session(session, owner)
        ensure_consolidation_in_session(
            session,
            owner,
            model_profile_version=self.model.fingerprint,
        )

    def fail(self, event, reason, *, terminal=False):
        """Dead inputs do not acquire new budgets when unrelated newer sources arrive."""
        actor = worker_actor(event)
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            row = self.outbox.require_claim_in_session(session, event.event_id, event.claim_epoch)
            dead = terminal or row.attempts + 1 >= 8
            if dead:
                ids = (row.processing_metadata or {}).get("snapshot_ids", [])
                statement = select(MemoryExtractionRow).where(
                    *owner_filter(MemoryExtractionRow, actor),
                    MemoryExtractionRow.disposition == "pending",
                    MemoryExtractionRow.extraction_revision.is_not(None),
                )
                if ids:
                    statement = statement.where(MemoryExtractionRow.extraction_id.in_(ids))
                elif "snapshot_ids" not in (row.processing_metadata or {}):
                    statement = statement.where(
                        MemoryExtractionRow.extraction_revision
                        <= event.payload.get("requested_revision", 0)
                    )
                else:
                    statement = statement.where(MemoryExtractionRow.extraction_id.in_([]))
                parts = list(session.scalars(statement))
                self._dispose(parts, "quarantined", reason, clear=False)
                if owner.active_consolidation_event_id == event.event_id:
                    owner.active_consolidation_event_id = None
            self.outbox.fail_in_session(
                session,
                event.event_id,
                event.claim_epoch,
                error=reason,
                max_attempts=8,
                terminal=terminal,
            )
            if dead:
                session.flush()
                advance_consolidated_revision_in_session(session, owner)
                ensure_consolidation_in_session(
                    session, owner, model_profile_version=self.model.fingerprint
                )
