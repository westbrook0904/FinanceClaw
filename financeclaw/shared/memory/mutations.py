"""The single SQL write boundary for memory, candidates and permanent idempotency."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.memory.authorization import require_scope
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.lifecycle import forget_record_in_session
from financeclaw.shared.memory.models import (
    EvidenceRef,
    MemoryActor,
    MemoryConflict,
    MemoryMutation,
    MemoryNotFound,
    MemoryOwnerSnapshot,
    MemoryPermissionError,
    MemoryReceipt,
    utc,
)
from financeclaw.shared.memory.policies import (
    LOW_RISK_VALUES,
    explicit_forget_intent,
    reject_nonasserted_profile,
    source_preferences,
    validate_memory_content,
    validate_profile_value,
)
from financeclaw.shared.memory.projection import enqueue_index_in_session, rebuild_digest_in_session
from financeclaw.shared.memory.repository import (
    canonical_hash,
    content_hash,
    current_record,
    lock_owner,
    owner_filter,
)
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow, MemorySourceRow


def receipt_id(actor: MemoryActor, mutation_id: str) -> str:
    """Scope every permanent operation identity to its authenticated owner."""
    return "memory-audit-" + canonical_hash([actor.tenant_id, actor.subject_id, mutation_id])


class MemoryMutationService:
    """Serialize owner facts, decisions, audit receipts and index intentions atomically."""

    def __init__(
        self,
        sessions: sessionmaker,
        *,
        auto_commit_low_risk: bool = True,
        candidate_seconds: int = 604800,
    ):
        """Reuse application SQL sessions and keep audit external delivery disabled."""
        self.sessions = sessions
        self.auto_commit_low_risk = auto_commit_low_risk
        if not 60 <= candidate_seconds <= 2592000:
            raise ValueError("candidate lifetime must be between one minute and thirty days")
        self.candidate_seconds = candidate_seconds
        self.audit = SqlAlchemyAuditRepository(sessions, emit_outbox=False)

    def apply(self, actor: MemoryActor, mutation: MemoryMutation) -> MemoryReceipt:
        """Commit one authenticated proposal or forget operation."""
        with self.sessions.begin() as session:
            return self.apply_in_session(session, actor, mutation)

    def apply_in_session(
        self, session: Session, actor: MemoryActor, mutation: MemoryMutation
    ) -> MemoryReceipt:
        """Run after business locks, then lock owner and check the permanent receipt first."""
        require_scope(actor, "memory:delete" if mutation.operation == "forget" else "memory:write")
        self._validate_actor_scope(actor, mutation)
        owner = lock_owner(session, actor)
        payload_hash = canonical_hash(mutation.model_dump(mode="json"))
        existing = self._replay(session, actor, mutation.mutation_id, payload_hash)
        if existing:
            return MemoryReceipt.model_validate(existing).model_copy(update={"replayed": True})
        receipt = self._apply(session, actor, owner, mutation, confirmed=False)
        self._audit(
            session,
            actor,
            mutation.mutation_id,
            payload_hash,
            receipt.model_dump(mode="json"),
            receipt.memory_id,
            receipt.revision,
            AuditEventType.MEMORY_PROPOSED
            if receipt.status == "proposed"
            else AuditEventType.MEMORY_DELETED
            if receipt.status == "forgotten"
            else AuditEventType.MEMORY_COMMITTED,
            tuple(ref.source_id for ref in mutation.evidence),
        )
        return receipt

    def decide(
        self,
        actor: MemoryActor,
        candidate_id: str,
        decision: str,
        mutation_id: str,
        expected_revision: int,
        content_hash: str,
    ) -> MemoryReceipt:
        """Make an independent candidate decision without creating or resuming a Turn."""
        with self.sessions.begin() as session:
            return self.decide_in_session(
                session, actor, candidate_id, decision, mutation_id, expected_revision, content_hash
            )

    def decide_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        candidate_id: str,
        decision: str,
        mutation_id: str,
        expected_revision: int,
        content_hash: str,
    ) -> MemoryReceipt:
        """Authorize first, replay durable decisions next, then validate fresh proposals."""
        if actor.kind != "user":
            raise MemoryPermissionError("only the authenticated user may decide a candidate")
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        owner = lock_owner(session, actor)
        candidate = current_record(session, actor, candidate_id)
        if candidate is None:
            raise MemoryNotFound("candidate is absent")
        require_scope(actor, "memory:delete" if candidate.operation == "forget" else "memory:write")
        payload_hash = canonical_hash(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "expected_revision": expected_revision,
                "content_hash": content_hash,
            }
        )
        existing = self._replay(session, actor, mutation_id, payload_hash)
        if existing:
            return MemoryReceipt.model_validate(existing).model_copy(update={"replayed": True})
        if (
            candidate.status != "proposed"
            or candidate.revision != expected_revision
            or candidate.content_hash != content_hash
        ):
            raise MemoryConflict("candidate no longer matches the reviewed proposal")
        if candidate.expires_at and utc(candidate.expires_at) <= datetime.now(UTC):
            raise MemoryConflict("candidate has expired")
        evidence = tuple(EvidenceRef.model_validate(ref) for ref in candidate.evidence)
        EvidenceReader().read_in_session(session, actor, evidence)
        if decision == "approve":
            target = current_record(session, actor, candidate.target_memory_id)
            if (target.revision if target else None) != candidate.expected_target_revision:
                raise MemoryConflict("candidate target revision changed")
            owner_revision_before = owner.memory_revision
            mutation = MemoryMutation(
                mutation_id=mutation_id,
                operation=candidate.operation,
                memory_id=candidate.target_memory_id,
                expected_revision=candidate.expected_target_revision,
                kind=candidate.kind,
                scope_type=candidate.scope_type,
                scope_id=candidate.scope_id,
                field=candidate.field,
                content=candidate.content,
                evidence=evidence,
                explicit_intent=True,
            )
            receipt = self._apply(session, actor, owner, mutation, confirmed=True)
            if owner.memory_revision == owner_revision_before:
                owner.memory_revision += 1
                receipt = receipt.model_copy(update={"owner_revision": owner.memory_revision})
        else:
            owner.memory_revision += 1
            receipt = MemoryReceipt(
                status="rejected",
                memory_id=candidate.target_memory_id or candidate.memory_id,
                revision=candidate.expected_target_revision or 0,
                candidate_id=candidate.memory_id,
                owner_revision=owner.memory_revision,
                privacy_epoch=owner.privacy_epoch,
            )
        self._append_version(
            session,
            owner,
            candidate,
            status="approved" if decision == "approve" else "rejected",
            mutation_id=mutation_id,
        )
        rebuild_digest_in_session(session, owner)
        self._audit(
            session,
            actor,
            mutation_id,
            payload_hash,
            receipt.model_dump(mode="json"),
            candidate_id,
            expected_revision,
            AuditEventType.MEMORY_DECIDED,
            tuple(ref.source_id for ref in evidence),
        )
        return receipt

    def update_settings(
        self,
        actor: MemoryActor,
        *,
        mutation_id: str,
        expected_policy_revision: int,
        read_enabled: bool | None = None,
        auto_enabled: bool | None = None,
    ) -> MemoryOwnerSnapshot:
        """Persist explicit settings with permanent replay and current policy revision checks."""
        with self.sessions.begin() as session:
            return self.update_settings_in_session(
                session,
                actor,
                mutation_id=mutation_id,
                expected_policy_revision=expected_policy_revision,
                read_enabled=read_enabled,
                auto_enabled=auto_enabled,
            )

    def update_settings_in_session(
        self,
        session: Session,
        actor: MemoryActor,
        *,
        mutation_id: str,
        expected_policy_revision: int,
        read_enabled: bool | None = None,
        auto_enabled: bool | None = None,
    ) -> MemoryOwnerSnapshot:
        """Revoke pending duties on policy changes and invalidate explicitly disabled reads."""
        require_scope(actor, "memory:write")
        if actor.kind != "user":
            raise MemoryPermissionError("only the authenticated user may change settings")
        owner = lock_owner(session, actor)
        payload_hash = canonical_hash(
            {
                "operation": "settings",
                "expected_policy_revision": expected_policy_revision,
                "read_enabled": read_enabled,
                "auto_enabled": auto_enabled,
            }
        )
        previous = self._replay(session, actor, mutation_id, payload_hash)
        if previous:
            return MemoryOwnerSnapshot.model_validate(previous["owner_snapshot"])
        if owner.policy_revision != expected_policy_revision:
            raise MemoryConflict("memory policy changed")
        if read_enabled is not None and owner.read_enabled != read_enabled:
            owner.read_enabled = read_enabled
            owner.privacy_epoch += 1
        if auto_enabled is not None:
            owner.auto_enabled = auto_enabled
        owner.policy_revision += 1
        if not owner.auto_enabled:
            for source in session.scalars(
                select(MemorySourceRow).where(
                    *owner_filter(MemorySourceRow, actor), MemorySourceRow.permit_revoked.is_(False)
                )
            ):
                source.permit_revoked = True
        snapshot = MemoryOwnerSnapshot.model_validate(owner)
        self._audit(
            session,
            actor,
            mutation_id,
            payload_hash,
            {"owner_snapshot": snapshot.model_dump(mode="json")},
            actor.subject_id,
            owner.policy_revision,
            AuditEventType.MEMORY_SETTINGS_UPDATED,
            (),
        )
        return snapshot

    def _apply(
        self,
        session: Session,
        actor: MemoryActor,
        owner: MemoryOwnerRow,
        mutation: MemoryMutation,
        *,
        confirmed: bool,
    ) -> MemoryReceipt:
        """Validate exact evidence, policy, target revision and source chronology before writing."""
        target_id = (
            mutation.memory_id
            or "memory-"
            + canonical_hash(
                [
                    actor.tenant_id,
                    actor.subject_id,
                    mutation.scope_type,
                    mutation.scope_id,
                    mutation.field if mutation.kind == "profile" else mutation.mutation_id,
                ]
            )[:48]
        )
        target = current_record(session, actor, target_id)
        if mutation.operation in {"update", "forget"}:
            if target is None:
                raise MemoryNotFound("target memory is absent")
            if target.revision != mutation.expected_revision or target.status != "active":
                raise MemoryConflict("target memory revision or status changed")
        elif target is not None and target.status == "active":
            if target.content != mutation.content:
                raise MemoryConflict("existing profile requires an explicit expected revision")
        if (
            target
            and mutation.operation != "forget"
            and (target.kind, target.scope_type, target.scope_id, target.field)
            != (mutation.kind, mutation.scope_type, mutation.scope_id, mutation.field)
        ):
            raise MemoryConflict("a memory update cannot change its kind, scope or profile field")
        if target and actor.kind == "tool":
            if (
                target.scope_type == "conversation"
                and target.scope_id != actor.conversation_id
                or target.scope_type == "agent"
                and target.scope_id != actor.agent_id
            ):
                raise MemoryPermissionError("target memory scope mismatch")
        if mutation.operation == "forget":
            mutation = mutation.model_copy(
                update={
                    "content": target.content,
                    "kind": target.kind,
                    "field": target.field,
                    "scope_type": target.scope_type,
                    "scope_id": target.scope_id,
                }
            )
        evidence = mutation.evidence
        if actor.kind != "user" and not evidence and mutation.operation != "forget":
            raise MemoryPermissionError("model writes require trusted evidence")
        documents = EvidenceReader().read_in_session(
            session, actor, evidence, for_derivation=actor.kind == "worker"
        )
        if mutation.operation != "forget":
            validate_memory_content(mutation.content)
        if mutation.kind == "profile" and mutation.operation != "forget":
            if not confirmed:
                reject_nonasserted_profile(documents)
            validate_profile_value(mutation.field, mutation.content)
            if any(
                document.source_kind not in {"user_message", "interaction_answer", "memory_action"}
                for document in documents
            ):
                raise MemoryPermissionError(
                    "assistant or tool output cannot establish a user profile"
                )
        for ref in evidence:
            source = session.scalars(
                select(MemorySourceRow).where(
                    *owner_filter(MemorySourceRow, actor),
                    MemorySourceRow.source_id == ref.source_id,
                )
            ).one()
            if source.reuse_blocked:
                raise MemoryPermissionError("forgotten sources cannot be reused")
        if actor.kind == "worker" and mutation.scope_type != "user":
            sources = tuple(
                session.scalars(
                    select(MemorySourceRow).where(
                        *owner_filter(MemorySourceRow, actor),
                        MemorySourceRow.source_id.in_([ref.source_id for ref in evidence]),
                    )
                )
            )
            if mutation.scope_type == "conversation" and any(
                source.conversation_id != mutation.scope_id for source in sources
            ):
                raise MemoryPermissionError("derived conversation scope does not match its sources")
            if mutation.scope_type == "agent":
                from financeclaw.shared.conversation.tables import ConversationRow

                conversations = tuple(
                    session.scalars(
                        select(ConversationRow).where(
                            *owner_filter(ConversationRow, actor),
                            ConversationRow.conversation_id.in_(
                                [source.conversation_id for source in sources]
                            ),
                        )
                    )
                )
                if not conversations or any(
                    conversation.agent_id != mutation.scope_id for conversation in conversations
                ):
                    raise MemoryPermissionError("derived agent scope does not match its sources")
        preference_sources = (
            tuple(
                document
                for document in documents
                if document.source_kind in {"user_message", "interaction_answer"}
                and source_preferences(document).get(mutation.field) == mutation.content
            )
            if mutation.kind == "profile" and mutation.field in LOW_RISK_VALUES
            else ()
        )
        latest_user = max(
            (
                document
                for document in documents
                if document.source_kind in {"user_message", "interaction_answer"}
            ),
            key=lambda document: document.ref.source_seq,
            default=None,
        )
        verified_preference = latest_user is not None and latest_user in preference_sources
        watermark = (
            max((document.ref.source_seq for document in preference_sources), default=0)
            if preference_sources
            else max((ref.source_seq for ref in evidence), default=owner.source_seq + 1)
        )
        if (
            target
            and target.forgotten_through_seq is not None
            and watermark <= target.forgotten_through_seq
        ):
            raise MemoryConflict("source predates the forget cutoff")
        if (
            target
            and target.status == "active"
            and evidence
            and watermark < target.source_watermark
        ):
            raise MemoryConflict("older evidence cannot override a newer user source")
        if mutation.operation == "forget":
            direct = (
                actor.kind == "user"
                and mutation.explicit_intent
                or actor.kind == "tool"
                and explicit_forget_intent(documents, target_id)
            )
        elif mutation.kind == "task":
            direct = True
        elif mutation.field in LOW_RISK_VALUES and self.auto_commit_low_risk:
            direct = actor.kind == "user" and mutation.explicit_intent or verified_preference
        else:
            direct = False
        if confirmed:
            direct = True
        if mutation.operation == "forget" and not direct:
            mutation = mutation.model_copy(
                update={
                    "content": target.content,
                    "kind": target.kind,
                    "field": target.field,
                    "scope_type": target.scope_type,
                    "scope_id": target.scope_id,
                }
            )
        proposed = not direct
        record_id = (
            "candidate-"
            + canonical_hash([actor.tenant_id, actor.subject_id, mutation.mutation_id])[:48]
            if proposed
            else target_id
        )
        revision = 1 if proposed or target is None else target.revision + 1
        if actor.kind == "user" and not evidence and mutation.operation != "forget":
            source = self._action_source(
                session, actor, owner, record_id, revision, mutation.content
            )
            evidence = (source,)
            watermark = source.source_seq
        if (
            target
            and target.status == "active"
            and mutation.operation != "forget"
            and not proposed
            and target.content == mutation.content
            and target.kind == mutation.kind
            and evidence
            and set(ref.source_id for ref in evidence).issubset(target.evidence_source_ids)
        ):
            return MemoryReceipt(
                status="committed",
                memory_id=target_id,
                revision=target.revision,
                owner_revision=owner.memory_revision,
                privacy_epoch=owner.privacy_epoch,
            )
        owner.memory_revision += 1
        if not proposed and target:
            if mutation.operation == "forget":
                forget_record_in_session(session, actor, owner, target)
            target.is_current = False
            session.flush()
        row = MemoryRecordRow(
            memory_id=record_id,
            revision=revision,
            tenant_id=actor.tenant_id,
            subject_id=actor.subject_id,
            owner_revision=owner.memory_revision,
            is_current=True,
            kind=mutation.kind,
            status="proposed"
            if proposed
            else "forgotten"
            if mutation.operation == "forget"
            else "active",
            scope_type=mutation.scope_type,
            scope_id=mutation.scope_id,
            field=mutation.field,
            content=mutation.content if mutation.operation != "forget" or proposed else "",
            content_hash=content_hash(mutation.content),
            evidence_source_ids=[ref.source_id for ref in evidence],
            evidence=[ref.model_dump(mode="json") for ref in evidence],
            source_watermark=watermark,
            mutation_id=mutation.mutation_id,
            operation=mutation.operation,
            target_memory_id=target_id if proposed else None,
            expected_target_revision=(target.revision if target else None) if proposed else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.candidate_seconds)
            if proposed
            else mutation.expires_at,
            forgotten_through_seq=owner.source_seq
            if mutation.operation == "forget" and not proposed
            else None,
        )
        session.add(row)
        session.flush()
        if proposed:
            from financeclaw.shared.notifications.memory import record_candidate

            record_candidate(session, row)
        else:
            enqueue_index_in_session(session, owner, row)
        rebuild_digest_in_session(session, owner)
        return MemoryReceipt(
            status="proposed"
            if proposed
            else "forgotten"
            if mutation.operation == "forget"
            else "committed",
            memory_id=target_id,
            revision=row.revision,
            candidate_id=row.memory_id if proposed else None,
            owner_revision=owner.memory_revision,
            privacy_epoch=owner.privacy_epoch,
            purge_status="pending" if row.status == "forgotten" else None,
        )

    def _action_source(
        self,
        session: Session,
        actor: MemoryActor,
        owner: MemoryOwnerRow,
        memory_id: str,
        revision: int,
        content: str,
    ) -> EvidenceRef:
        """Bind a manual action to its displayed authoritative record without copying text."""
        owner.source_seq += 1
        source_id = (
            "source-"
            + canonical_hash([actor.tenant_id, actor.subject_id, memory_id, revision])[:48]
        )
        row = MemorySourceRow(
            source_id=source_id,
            tenant_id=actor.tenant_id,
            subject_id=actor.subject_id,
            source_seq=owner.source_seq,
            source_kind="memory_action",
            object_id=f"{memory_id}:{revision}",
            source_version=1,
            content_hash=content_hash(content),
            conversation_id=actor.conversation_id,
            turn_id=actor.turn_id,
            visible=True,
            version_valid=True,
            reuse_blocked=False,
            permit=None,
            data_classification=actor.data_classification.value,
            processing_region=actor.processing_region,
            permit_revoked=False,
        )
        session.add(row)
        return EvidenceRef(
            source_id=source_id,
            source_kind=row.source_kind,
            source_version=1,
            content_hash=row.content_hash,
            source_seq=row.source_seq,
        )

    def _append_version(
        self, session: Session, owner: MemoryOwnerRow, previous: MemoryRecordRow, **changes
    ) -> MemoryRecordRow:
        """Append decision history after flushing the old current-head transition."""
        values = {
            column.key: getattr(previous, column.key)
            for column in MemoryRecordRow.__table__.columns
        }
        previous.is_current = False
        session.flush()
        values.update(
            revision=previous.revision + 1,
            owner_revision=owner.memory_revision,
            is_current=True,
            created_at=datetime.now(UTC),
            **changes,
        )
        row = MemoryRecordRow(**values)
        session.add(row)
        session.flush()
        return row

    def _validate_actor_scope(self, actor: MemoryActor, mutation: MemoryMutation) -> None:
        """Prevent a model from expanding an agent or conversation scoped write."""
        if actor.kind in {"user", "worker"}:
            return
        if mutation.scope_type == "agent" and mutation.scope_id != actor.agent_id:
            raise MemoryPermissionError("agent memory scope mismatch")
        if mutation.scope_type == "conversation" and mutation.scope_id != actor.conversation_id:
            raise MemoryPermissionError("conversation memory scope mismatch")

    def _replay(self, session: Session, actor: MemoryActor, mutation_id: str, payload_hash: str):
        """Return the permanent minimal result without reevaluating historical source state."""
        row = session.scalars(
            select(AuditRecordRow).where(
                *owner_filter(AuditRecordRow, actor),
                AuditRecordRow.audit_id == receipt_id(actor, mutation_id),
            )
        ).one_or_none()
        if row is None:
            return None
        if row.payload_hash != payload_hash:
            raise MemoryConflict("mutation ID identifies a different operation")
        return row.metadata_json["result"]

    def _audit(
        self,
        session: Session,
        actor: MemoryActor,
        mutation_id: str,
        payload_hash: str,
        result: dict,
        resource_id: str,
        revision: int,
        event_type: AuditEventType,
        evidence: tuple[str, ...],
    ) -> None:
        """Store no fact body in the permanent idempotency record."""
        self.audit.append_in_session(
            session,
            AuditRecord(
                audit_id=receipt_id(actor, mutation_id),
                event_type=event_type,
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                conversation_id=actor.conversation_id,
                turn_id=actor.turn_id,
                tool_call_id=actor.tool_call_id,
                resource_type="memory",
                resource_id=resource_id,
                resource_version=str(revision),
                action=event_type.value,
                decision=result.get("status", "committed"),
                policy_version="memory/1",
                payload_hash=payload_hash,
                evidence_refs=evidence,
                metadata={"result": result},
            ),
        )
