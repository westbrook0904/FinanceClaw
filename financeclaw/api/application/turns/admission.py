"""Atomic product admission. No graph, native client or network call belongs here."""

from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.responses import TurnAccepted
from financeclaw.shared.conversation.repository import (
    ConversationConflict,
    ConversationNotFound,
    IdempotencyConflict,
    content_hash,
)
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.turns.audit import record_grant
from financeclaw.shared.turns.authorization import bounded_authorization, require_scopes
from financeclaw.shared.turns.snapshots import agent_snapshot
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, digest, now


def accepted(turn, *, replay=False):
    """Project an accepted Turn without exposing native submission parameters."""
    return TurnAccepted(
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        status=turn.status,
        revision=turn.revision,
        idempotent_replay=replay,
    )


class TurnAdmission:
    """Serialize one active task per conversation with atomic input and command persistence."""

    def __init__(self, service):
        """Inject dependencies without starting background work."""
        self.service = service

    def accept(
        self,
        conversation_id,
        request,
        *,
        tenant_id,
        subject_id,
        scopes,
        idempotency_key,
        authorization=None,
        notification_address=None,
    ):
        """Persist input, finite grant and one immutable start command before returning 202."""
        service = self.service
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise IdempotencyConflict("a bounded idempotency key is required")
        conversation = service.journal.get_owned(conversation_id, tenant_id, subject_id)
        profile = service.releases.agents.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
        service.releases.require_root(profile)
        require_scopes(scopes, profile.required_scopes)
        evidence, expires = bounded_authorization(
            service.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )
        fingerprint = digest([conversation_id, request.message, profile.agent_id, profile.version])

        def replay(session):
            """Reuse only the original owner, request fingerprint and delivery target."""
            previous = session.scalar(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.tenant_id == tenant_id,
                    ConversationTurnRow.subject_id == subject_id,
                    ConversationTurnRow.idempotency_key == idempotency_key,
                )
            )
            if previous is not None:
                if (
                    previous.request_hash != fingerprint
                    or previous.conversation_id != conversation_id
                ):
                    raise IdempotencyConflict("idempotency key belongs to a different request")
                if notification_address:
                    from financeclaw.shared.notifications.facts import bind_target

                    bind_target(
                        session,
                        previous,
                        notification_address,
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        evidence=evidence,
                        replay=True,
                    )
                return accepted(previous, replay=True)
            return None

        try:
            with service.sessions.begin() as session:
                # A conversation serializes its message sequence and active Turn admission.
                session.execute(
                    update(ConversationRow)
                    .where(ConversationRow.conversation_id == conversation_id)
                    .values(updated_at=ConversationRow.updated_at)
                )
                current = session.get(ConversationRow, conversation_id)
                if current is None or (current.tenant_id, current.subject_id) != (
                    tenant_id,
                    subject_id,
                ):
                    raise ConversationNotFound("conversation not found")
                result = replay(session)
                if result:
                    return result
                if current.status != "active":
                    raise ConversationConflict("conversation is not active")
                if (current.agent_id, current.agent_profile_version) != (
                    profile.agent_id,
                    profile.version,
                ):
                    raise ConversationConflict("conversation release changed during admission")
                previous = session.scalar(
                    select(ConversationTurnRow)
                    .where(ConversationTurnRow.conversation_id == conversation_id)
                    .order_by(ConversationTurnRow.created_at.desc())
                    .limit(1)
                )
                if previous and previous.status not in TERMINAL_STATUSES:
                    raise ConversationConflict("conversation already has an active Turn")
                if previous and previous.status in {"failed", "cancelled"}:
                    current.agent_thread_id = str(uuid4())
                turn_id, message_id, command_id = (str(uuid4()) for _ in range(3))
                context = ExecutionContext(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    scopes=scopes,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    request_clock=now().isoformat(),
                    data_classification=profile.data_classification,
                )
                sequence = (
                    session.scalar(
                        select(func.max(ConversationMessageRow.sequence)).where(
                            ConversationMessageRow.conversation_id == conversation_id
                        )
                    )
                    or 0
                ) + 1
                release = agent_snapshot(
                    profile, context, thread_id=current.agent_thread_id, input_hash=fingerprint
                )
                release.update(user_message_id=message_id, user_message_sequence=sequence)
                turn = ConversationTurnRow(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    user_message_id=message_id,
                    thread_id=current.agent_thread_id,
                    release_snapshot=release,
                    release_hash=digest(release),
                    current_command_id=command_id,
                    grant_scopes=sorted(scopes),
                    grant_source=evidence.source,
                    grant_source_hash=evidence.source_hash,
                    grant_issued_at=evidence.issued_at,
                    grant_expires_at=expires,
                )
                session.add(turn)
                session.flush()
                session.add(
                    ConversationMessageRow(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        sequence=sequence,
                        role="user",
                        content=request.message,
                        content_hash=content_hash(request.message),
                    )
                )
                payload = {
                    "input": {
                        "messages": [
                            {
                                "role": "user",
                                "content": request.message,
                                "id": message_id,
                                "additional_kwargs": {
                                    "financeclaw_source": {
                                        "conversation_id": conversation_id,
                                        "turn_id": turn_id,
                                        "sequence": sequence,
                                    }
                                },
                            }
                        ]
                    }
                }
                session.add(
                    TurnCommandRow(
                        command_id=command_id,
                        turn_id=turn_id,
                        sequence=1,
                        kind="start",
                        request_payload=payload,
                        request_hash=digest(payload),
                        grant_revision=turn.grant_revision,
                        authorized_scopes=sorted(scopes),
                    )
                )
                current.updated_at = now()
                session.flush()
                if notification_address:
                    from financeclaw.shared.notifications.facts import bind_target

                    bind_target(
                        session,
                        turn,
                        notification_address,
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        evidence=evidence,
                        replay=False,
                    )
                record_grant(session, turn)
                service.store.wake(session, turn)
                from financeclaw.shared.notifications.facts import record_progress

                record_progress(session, turn)
                return accepted(turn)
        except IntegrityError:
            # Covers races involving the same principal's key in different conversations.
            with service.sessions() as session:
                result = replay(session)
                if result:
                    return result
            raise ConversationConflict("concurrent Turn admission conflicted") from None
