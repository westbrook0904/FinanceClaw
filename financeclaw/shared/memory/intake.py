"""Transactional source admission and durable, bounded Turn-closure extraction intent."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.memory.evidence import interaction_content
from financeclaw.shared.memory.models import (
    MemoryActor,
    MemoryConflict,
    MemoryDerivationPermit,
    MemoryMutation,
    MemoryNotFound,
    MemorySource,
)
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.policies import explicit_preferences, interaction_preferences
from financeclaw.shared.memory.repository import (
    canonical_hash,
    content_hash,
    lock_owner,
    owner_filter,
    source_snapshot,
)
from financeclaw.shared.memory.tables import MemoryRecordRow, MemorySourceRow
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow


class MemoryIntake:
    """Register references with business writes; perform no model work in API transactions."""

    def __init__(
        self,
        sessions: sessionmaker,
        *,
        auto_commit_low_risk: bool = True,
        enabled: bool = True,
        permit_seconds: int = 86400,
        candidate_seconds: int = 604800,
    ):
        """Apply tenant defaults while preserving explicit per-owner settings."""
        self.sessions = sessions
        self.enabled = enabled
        self.auto_commit_low_risk = auto_commit_low_risk
        if not 60 <= permit_seconds <= 86400:
            raise ValueError("derive permit must be between one minute and one day")
        self.permit_seconds = permit_seconds
        self.mutations = MemoryMutationService(
            sessions, auto_commit_low_risk=auto_commit_low_risk, candidate_seconds=candidate_seconds
        )

    def register_message_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        message_id: str,
        *,
        allow_derivation: bool = True,
    ) -> MemorySource:
        """Validate the original Journal user message before registering an exact version."""
        session.flush()
        message = session.scalars(
            select(ConversationMessageRow)
            .join(
                ConversationRow,
                ConversationRow.conversation_id == ConversationMessageRow.conversation_id,
            )
            .where(
                *owner_filter(ConversationRow, actor),
                ConversationMessageRow.message_id == message_id,
                ConversationMessageRow.role == "user",
                ConversationMessageRow.visible.is_(True),
            )
        ).one_or_none()
        if message is None:
            raise MemoryNotFound("user message source is unavailable")
        source, newly_admitted = self._register(
            session,
            actor,
            "user_message",
            message_id,
            1,
            message.content,
            message.conversation_id,
            message.turn_id,
            allow_derivation=allow_derivation,
        )
        owner = lock_owner(session, actor)
        if (
            newly_admitted
            and allow_derivation
            and self.auto_commit_low_risk
            and owner.auto_enabled
            and self.enabled
            and ("memory:write" in actor.scopes or "*" in actor.scopes)
        ):
            for field, value in explicit_preferences(message.content).items():
                target = session.scalars(
                    select(MemoryRecordRow).where(
                        *owner_filter(MemoryRecordRow, actor),
                        MemoryRecordRow.kind == "profile",
                        MemoryRecordRow.field == field.value,
                        MemoryRecordRow.scope_type == "user",
                        MemoryRecordRow.is_current.is_(True),
                        MemoryRecordRow.status == "active",
                    )
                ).one_or_none()
                self.mutations.apply_in_session(
                    session,
                    actor,
                    MemoryMutation(
                        mutation_id=f"source-preference:{source.source_id}:{field.value}",
                        operation="update" if target else "create",
                        memory_id=target.memory_id if target else None,
                        expected_revision=target.revision if target else None,
                        kind="profile",
                        field=field,
                        content=value,
                        evidence=(source.evidence_ref(),),
                        explicit_intent=True,
                    ),
                )
        return source

    def register_interaction_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        interaction_id: str,
        *,
        allow_derivation: bool = True,
    ) -> MemorySource:
        """Bind a verified answer to its complete original question and accepted identity."""
        session.flush()
        result = session.execute(
            select(InteractionRow, ConversationTurnRow)
            .join(ConversationTurnRow, ConversationTurnRow.turn_id == InteractionRow.turn_id)
            .where(
                *owner_filter(ConversationTurnRow, actor),
                InteractionRow.interaction_id == interaction_id,
            )
        ).one_or_none()
        if (
            result is None
            or result[0].response is None
            or result[0].status not in {"resolved", "rejected"}
            or result[0].decided_by != actor.subject_id
        ):
            raise MemoryNotFound("accepted user interaction is unavailable")
        interaction, turn = result
        source, newly_admitted = self._register(
            session,
            actor,
            "interaction_answer",
            interaction_id,
            interaction.revision,
            interaction_content(interaction),
            turn.conversation_id,
            turn.turn_id,
            allow_derivation=allow_derivation,
        )
        owner = lock_owner(session, actor)
        if (
            newly_admitted
            and allow_derivation
            and self.auto_commit_low_risk
            and self.enabled
            and owner.auto_enabled
            and ("memory:write" in actor.scopes or "*" in actor.scopes)
        ):
            for field, value in interaction_preferences(interaction_content(interaction)).items():
                target = session.scalars(
                    select(MemoryRecordRow).where(
                        *owner_filter(MemoryRecordRow, actor),
                        MemoryRecordRow.kind == "profile",
                        MemoryRecordRow.field == field.value,
                        MemoryRecordRow.scope_type == "user",
                        MemoryRecordRow.is_current.is_(True),
                        MemoryRecordRow.status == "active",
                    )
                ).one_or_none()
                self.mutations.apply_in_session(
                    session,
                    actor,
                    MemoryMutation(
                        mutation_id=f"source-preference:{source.source_id}:{field.value}",
                        operation="update" if target else "create",
                        memory_id=target.memory_id if target else None,
                        expected_revision=target.revision if target else None,
                        kind="profile",
                        field=field,
                        content=value,
                        evidence=(source.evidence_ref(),),
                        explicit_intent=True,
                    ),
                )
        return source

    def close_turn_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        turn_id: str,
        *,
        pipeline_version: str = "memory/1",
        model_profile_version: str = "default",
        final_message_id: str | None = None,
    ) -> str | None:
        """Commit final Journal source references and one deterministic prepare intention."""
        session.flush()
        turn = session.scalars(
            select(ConversationTurnRow).where(
                *owner_filter(ConversationTurnRow, actor), ConversationTurnRow.turn_id == turn_id
            )
        ).one_or_none()
        if turn is None:
            raise MemoryNotFound("turn is absent")
        if turn.status != "completed":
            return None
        owner = lock_owner(session, actor)
        if not owner.auto_enabled or not self.enabled:
            return None
        sources = tuple(
            session.scalars(
                select(MemorySourceRow)
                .where(
                    *owner_filter(MemorySourceRow, actor),
                    MemorySourceRow.turn_id == turn_id,
                    MemorySourceRow.source_kind != "assistant_message",
                    MemorySourceRow.permit.is_not(None),
                    MemorySourceRow.permit_revoked.is_(False),
                    MemorySourceRow.visible.is_(True),
                    MemorySourceRow.version_valid.is_(True),
                    MemorySourceRow.reuse_blocked.is_(False),
                )
                .order_by(MemorySourceRow.source_seq)
                .limit(129)
            )
        )
        if not sources:
            return None
        if len(sources) > 127:
            event_id = "memory-prepare-" + canonical_hash(
                [actor.tenant_id, actor.subject_id, turn_id, pipeline_version]
            )
            if session.get(OutboxEventRow, event_id) is None:
                session.add(
                    OutboxEventRow(
                        event_id=event_id,
                        destination="memory_extract",
                        event_type="memory.extract.skipped",
                        aggregate_type="turn",
                        aggregate_id=turn_id,
                        tenant_id=actor.tenant_id,
                        subject_id=actor.subject_id,
                        payload={
                            "turn_id": turn_id,
                            "pipeline_version": pipeline_version,
                            "model_profile_version": model_profile_version,
                        },
                        processing_metadata={"outcome": "source_limit_exceeded"},
                        status="published",
                        attempts=0,
                        available_at=datetime.now(UTC),
                        published_at=datetime.now(UTC),
                    )
                )
            return event_id
        final_query = select(ConversationMessageRow).where(
            ConversationMessageRow.turn_id == turn_id,
            ConversationMessageRow.role == "assistant",
            ConversationMessageRow.parent_message_id.is_(None),
            ConversationMessageRow.visible.is_(True),
        )
        if final_message_id:
            final_query = final_query.where(ConversationMessageRow.message_id == final_message_id)
        final = session.scalars(final_query).one_or_none()
        if final is None:
            raise MemoryConflict("completed turn requires a final Journal message")
        source_refs = [source_snapshot(source).evidence_ref() for source in sources]
        if not final.skill_access_refs:
            assistant, _ = self._register(
                session,
                actor,
                "assistant_message",
                final.message_id,
                1,
                final.content,
                turn.conversation_id,
                turn_id,
                allow_derivation=True,
            )
            source_refs.append(assistant.evidence_ref())
        payload = {
            "sources": [ref.model_dump(mode="json") for ref in source_refs],
            "turn_id": turn_id,
            "conversation_id": turn.conversation_id,
            "final_message_id": final.message_id,
            "final_message_hash": final.content_hash,
            "pipeline_version": pipeline_version,
            "model_profile_version": model_profile_version,
            "schema_version": "memory-v1",
        }
        event_id = "memory-prepare-" + canonical_hash(
            [actor.tenant_id, actor.subject_id, turn_id, pipeline_version]
        )
        existing = session.get(OutboxEventRow, event_id)
        if existing:
            if existing.payload != payload:
                raise MemoryConflict("closed memory input changed")
            return event_id
        session.add(
            OutboxEventRow(
                event_id=event_id,
                destination="memory_extract",
                event_type="memory.extract.prepare",
                aggregate_type="turn",
                aggregate_id=turn_id,
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                payload=payload,
                status="pending",
                attempts=0,
                available_at=datetime.now(UTC),
            )
        )
        return event_id

    def _register(
        self,
        session: Session,
        actor: MemoryActor,
        kind: str,
        object_id: str,
        version: int,
        content: str,
        conversation_id: str | None,
        turn_id: str | None,
        *,
        allow_derivation: bool,
    ) -> tuple[MemorySource, bool]:
        """Allocate user source order once and bind a finite, non-secret derivation permit."""
        owner = lock_owner(session, actor)
        digest = content_hash(content)
        source_id = (
            "source-"
            + canonical_hash([actor.tenant_id, actor.subject_id, kind, object_id, version])[:48]
        )
        existing = session.scalars(
            select(MemorySourceRow).where(
                *owner_filter(MemorySourceRow, actor), MemorySourceRow.source_id == source_id
            )
        ).one_or_none()
        if existing:
            if existing.content_hash != digest:
                raise MemoryConflict("immutable source version changed")
            return source_snapshot(existing), False
        owner.source_seq += 1
        turn = session.get(ConversationTurnRow, turn_id) if turn_id else None
        frozen_context = (turn.release_snapshot or {}).get("context", {}) if turn else {}
        classification = frozen_context.get("data_classification", actor.data_classification.value)
        processing_region = frozen_context.get("processing_region", actor.processing_region)
        permit = None
        if (
            self.enabled
            and allow_derivation
            and owner.auto_enabled
            and ("memory:write" in actor.scopes or "*" in actor.scopes)
        ):
            permit = MemoryDerivationPermit(
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                source_id=source_id,
                source_version=version,
                content_hash=digest,
                policy_revision=owner.policy_revision,
                data_classification=classification,
                processing_region=processing_region,
                authorization_hash=canonical_hash(
                    [actor.tenant_id, actor.subject_id, sorted(actor.scopes), actor.agent_id]
                ),
                expires_at=datetime.now(UTC) + timedelta(seconds=self.permit_seconds),
            ).model_dump(mode="json")
        row = MemorySourceRow(
            source_id=source_id,
            tenant_id=actor.tenant_id,
            subject_id=actor.subject_id,
            source_seq=owner.source_seq,
            source_kind=kind,
            object_id=object_id,
            source_version=version,
            content_hash=digest,
            conversation_id=conversation_id,
            turn_id=turn_id,
            permit=permit,
            data_classification=classification,
            processing_region=processing_region,
            visible=True,
            version_valid=True,
            reuse_blocked=False,
            permit_revoked=False,
        )
        session.add(row)
        session.flush()
        return source_snapshot(row), True
