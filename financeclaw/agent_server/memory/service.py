"""Thin execution adapters over the shared SQL memory domain and native retrieval index."""

from sqlalchemy import select

from financeclaw.agent_server.context.turns import user_anchor
from financeclaw.agent_server.memory.recall import MemoryRecall
from financeclaw.shared.memory.models import (
    MemoryActor,
    MemoryMutation,
    MemoryNotFound,
    MemoryPermissionError,
)
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import MemoryRepository, owner_filter, source_snapshot
from financeclaw.shared.memory.tables import MemorySourceRow


class LongTermMemoryService:
    """Verify ToolRuntime identity, resolve trusted evidence and delegate transactional writes."""

    def __init__(
        self,
        *,
        sessions,
        conversation_repository,
        index_version="memory-v1",
        auto_commit_low_risk=True,
        candidate_seconds=604800,
        enabled=True,
    ):
        """Share the application database without retaining a second memory write implementation."""
        self.conversations = conversation_repository
        self.repository = MemoryRepository(sessions)
        self.mutations = MemoryMutationService(
            sessions, auto_commit_low_risk=auto_commit_low_risk, candidate_seconds=candidate_seconds
        )
        self.enabled = enabled
        self.recall = MemoryRecall(self.repository, index_version=index_version)

    def actor(self, context, *, tool_call_id=None):
        """Only an authenticated execution snapshot can establish the tool's actor and scope."""
        self.conversations.execution.verify_context(context)
        snapshot = self.conversations.execution.get(context.turn_id)["release_snapshot"]
        return MemoryActor(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            scopes=context.scopes,
            kind="tool",
            turn_id=context.turn_id,
            conversation_id=context.conversation_id,
            tool_call_id=tool_call_id,
            agent_id=snapshot.get("profile", {}).get("agent_id"),
            data_classification=context.data_classification,
            processing_region=context.processing_region,
        )

    def privacy_epoch(self, context):
        """Read current invalidation even when the owner has disabled memory recall."""
        return self.repository.owner_snapshot(self.actor(context)).privacy_epoch

    def snapshot(self, context):
        """Read the version boundary used to freeze this Turn's memory inputs."""
        result = self.repository.owner_snapshot(self.actor(context))
        return result if self.enabled else result.model_copy(update={"read_enabled": False})

    def profile(self, context):
        """Read only the finite registered profile fields for the trusted scope."""
        return self.repository.list_records(self.actor(context), kind="profile", limit=100)

    def l0_snapshot(self, context, *, limit=6):
        """Read consistent profile and directory versions under a short shared owner lock."""
        from financeclaw.shared.memory.tables import MemoryOwnerRow

        actor = self.actor(context)
        with self.repository.sessions.begin() as session:
            owner = session.scalar(
                select(MemoryOwnerRow)
                .where(*owner_filter(MemoryOwnerRow, actor))
                .with_for_update(read=True)
            )
            snapshot = self.repository.owner_snapshot_in_session(session, actor)
            if owner is None or not snapshot.read_enabled:
                return snapshot, (), ()
            profiles = self.repository.list_records_in_session(
                session, actor, kind="profile", limit=100
            )
            tasks = []
            for entry in snapshot.digest.get("tasks", ()):
                try:
                    task = self.repository.get_in_session(
                        session, actor, entry["memory_id"], revision=entry["revision"]
                    )
                except MemoryNotFound:
                    continue
                tasks.append(task)
                if len(tasks) >= limit:
                    break
            return snapshot, profiles, tuple(tasks)

    def get(self, context, memory_id, *, revision=None):
        """Return no deleted or revoked source text, including from a frozen old revision."""
        try:
            return self.repository.get(self.actor(context), memory_id, revision=revision)
        except MemoryNotFound:
            return None

    def search(self, context, store, *, query=None, limit=6):
        """Search derived IDs and return current SQL facts, with bounded keyword fallback."""
        if not self.enabled:
            raise MemoryPermissionError("memory is disabled by deployment policy")
        return self.recall.search(self.actor(context), store, query=query, limit=limit)

    def task_query(self, context, user_text):
        """Include accepted clarification semantics in on-demand retrieval without another model."""
        from financeclaw.shared.memory.evidence import EvidenceReader

        actor = self.actor(context)
        with self.repository.sessions() as session:
            rows = tuple(
                session.scalars(
                    select(MemorySourceRow)
                    .where(
                        *owner_filter(MemorySourceRow, actor),
                        MemorySourceRow.turn_id == context.turn_id,
                        MemorySourceRow.source_kind == "interaction_answer",
                        MemorySourceRow.visible.is_(True),
                        MemorySourceRow.version_valid.is_(True),
                    )
                    .order_by(MemorySourceRow.source_seq.desc())
                    .limit(8)
                )
            )
            refs = tuple(source_snapshot(row).evidence_ref() for row in reversed(rows))
            documents = EvidenceReader().read_in_session(session, actor, refs)
            return user_text + "\n" + "\n".join(document.content for document in documents)

    def accepted_profile_changes(self, context):
        """Return current fields whose newest supporting user evidence is this Turn's answer."""
        actor = self.actor(context)
        with self.repository.sessions() as session:
            sources = {
                row.source_id: row.source_seq
                for row in session.scalars(
                    select(MemorySourceRow)
                    .where(
                        *owner_filter(MemorySourceRow, actor),
                        MemorySourceRow.turn_id == context.turn_id,
                        MemorySourceRow.source_kind == "interaction_answer",
                        MemorySourceRow.visible.is_(True),
                        MemorySourceRow.version_valid.is_(True),
                    )
                    .order_by(MemorySourceRow.source_seq.desc())
                    .limit(128)
                )
            }
            return tuple(
                row
                for row in self.repository.list_records_in_session(
                    session, actor, kind="profile", limit=100
                )
                if any(sources.get(ref.source_id) == row.source_watermark for ref in row.evidence)
            )

    def evidence(self, context, identifiers):
        """Resolve registered source or original message IDs, never model-provided ownership."""
        actor = self.actor(context)
        anchor = user_anchor(context, self.conversations)
        refs = []
        with self.repository.sessions() as session:
            for identifier in identifiers:
                if identifier == "current_answers":
                    rows = session.scalars(
                        select(MemorySourceRow)
                        .where(
                            *owner_filter(MemorySourceRow, actor),
                            MemorySourceRow.turn_id == context.turn_id,
                            MemorySourceRow.source_kind == "interaction_answer",
                        )
                        .order_by(MemorySourceRow.source_seq)
                        .limit(32)
                    )
                    refs.extend(source_snapshot(row).evidence_ref() for row in rows)
                    continue
                identifier = anchor if identifier == "current" else identifier
                row = session.scalar(
                    select(MemorySourceRow).where(
                        *owner_filter(MemorySourceRow, actor),
                        (MemorySourceRow.source_id == identifier)
                        | (MemorySourceRow.object_id == identifier),
                    )
                )
                if row is None:
                    raise MemoryPermissionError("evidence must identify a registered user source")
                refs.append(source_snapshot(row).evidence_ref())
        return tuple({ref.source_id: ref for ref in refs}.values())

    def save(
        self,
        context,
        *,
        tool_call_id,
        kind,
        content,
        evidence_ids,
        field=None,
        memory_id=None,
        expected_revision=None,
        scope_type="conversation",
        scope_id=None,
        expires_at=None,
    ):
        """Return committed or proposed; memory decisions never resume a native interrupt."""
        if not self.enabled:
            raise MemoryPermissionError("memory is disabled by deployment policy")
        actor = self.actor(context, tool_call_id=tool_call_id)
        return self.mutations.apply(
            actor,
            MemoryMutation(
                mutation_id=f"tool:{context.turn_id}:{tool_call_id}:save",
                operation="update" if memory_id else "create",
                memory_id=memory_id,
                expected_revision=expected_revision,
                kind=kind,
                field=field,
                content=content,
                scope_type=scope_type,
                scope_id=scope_id
                if scope_id is not None
                else (context.conversation_id if scope_type == "conversation" else ""),
                evidence=self.evidence(context, evidence_ids),
                expires_at=expires_at,
            ),
        )

    def forget(self, context, memory_id, *, tool_call_id, expected_revision):
        """Create a reviewed candidate when model deletion intent is unverified."""
        actor = self.actor(context, tool_call_id=tool_call_id)
        return self.mutations.apply(
            actor,
            MemoryMutation(
                mutation_id=f"tool:{context.turn_id}:{tool_call_id}:forget",
                operation="forget",
                memory_id=memory_id,
                expected_revision=expected_revision,
                evidence=self.evidence(context, ("current",)),
            ),
        )
