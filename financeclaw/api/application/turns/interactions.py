"""All channels converge on the same immutable decision and resume command."""

from contextlib import nullcontext
from uuid import uuid4

from jsonschema import ValidationError
from sqlalchemy import select

from financeclaw.api.application.turns.waits import native_response
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.audit import record_grant
from financeclaw.shared.turns.authorization import (
    bounded_authorization,
    check_authorization,
    intersect_scopes,
    require_scopes,
)
from financeclaw.shared.turns.projection import public_interaction
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow, TurnCommandRow
from financeclaw.shared.turns.types import (
    TERMINAL_STATUSES,
    InteractionConflict,
    InteractionNotFound,
    aware,
    digest,
    now,
)


class TurnInteractions:
    """Bind typed human decisions to immutable interrupts and successor commands."""

    def __init__(self, service):
        """Inject dependencies without starting background work."""
        self.service = service

    def owned(self, session, interaction_id, tenant_id, subject_id, conversation_id=None):
        """Check ownership and the conversation-to-Turn relationship before accessing state."""
        row = session.scalar(
            select(InteractionRow)
            .join(ConversationTurnRow)
            .where(
                InteractionRow.interaction_id == interaction_id,
                ConversationTurnRow.tenant_id == tenant_id,
                ConversationTurnRow.subject_id == subject_id,
            )
        )
        if row is None:
            raise InteractionNotFound("interaction not found")
        turn = session.get(ConversationTurnRow, row.turn_id)
        if conversation_id is not None and turn.conversation_id != conversation_id:
            raise InteractionNotFound("interaction not found")
        return row

    def get_owned(self, interaction_id, tenant_id, subject_id):
        """Project a governed interaction after checking its Turn owner."""
        with self.service.sessions() as session:
            return public_interaction(self.owned(session, interaction_id, tenant_id, subject_id))

    def channel_state(self, conversation_id, *, tenant_id, subject_id, response_key):
        """Retain the original meaning of a redelivered message after subsequent waits."""
        self.service.journal.get_owned(conversation_id, tenant_id, subject_id)
        with self.service.sessions() as session:
            answered = session.scalar(
                select(InteractionRow)
                .join(ConversationTurnRow)
                .where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    InteractionRow.response_key == response_key,
                )
            )
            if answered:
                return {"answered": public_interaction(answered)}
            if session.scalar(
                select(ConversationTurnRow.turn_id).where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    ConversationTurnRow.idempotency_key == response_key,
                )
            ):
                return {"turn_replay": True}
            turn = session.scalar(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                )
            )
            if turn is None:
                return {}
            rows = list(
                session.scalars(
                    select(InteractionRow).where(
                        InteractionRow.turn_id == turn.turn_id,
                        InteractionRow.status.in_(("pending", "expired")),
                    )
                )
            )
            items = []
            expired = False
            for row in rows:
                item = public_interaction(row)
                if aware(row.expires_at) <= now():
                    item["status"] = "expired"
                    expired = True
                items.append(item)
            return {
                "turn_id": turn.turn_id,
                "reason": "interaction_expired" if expired else turn.status_reason,
                "interactions": items,
            }

    async def respond(self, interaction_id, response, **kwargs):
        """Persist a human response before waking command observation."""
        result = await run_sync(self.accept_response, interaction_id, response, **kwargs)
        self.service.wake()
        return result

    def accept_response(
        self,
        interaction_id,
        response,
        *,
        tenant_id,
        subject_id,
        scopes,
        idempotency_key,
        conversation_id=None,
        authorization=None,
        session=None,
    ):
        """Commit one typed answer, narrowed grant and immutable resume command together."""
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise InteractionConflict("bounded response idempotency key is required")
        with (
            nullcontext(session) if session is not None else self.service.sessions.begin()
        ) as session:
            saved = self.owned(session, interaction_id, tenant_id, subject_id, conversation_id)
            turn = self.service.store.lock(session, saved.turn_id)
            session.refresh(saved)
            request = saved.request
            point = InteractionPoint.model_validate(request["point"])
            normalized = response
            if point.kind != "approval":
                try:
                    normalized = response.model_copy(
                        update={"answer": point.normalize_answer(response.answer)}
                    )
                except (ValueError, TypeError, ValidationError) as exc:
                    raise InteractionConflict("answer does not match the frozen schema") from exc
            if (
                response.revision != saved.revision
                or response.kind != point.kind
                or response.action_hash != request["action_hash"]
                or point.kind == "approval"
                and response.decision not in request["allowed_decisions"]
            ):
                raise InteractionConflict("response does not match the frozen interaction")
            body = normalized.model_dump(mode="json")
            fingerprint = digest(body)
            if saved.response is not None:
                if saved.response_key != idempotency_key or saved.response_hash != fingerprint:
                    raise InteractionConflict("interaction already has a different decision")
                return public_interaction(saved)
            if (
                saved.status != "pending"
                or aware(saved.expires_at) <= now()
                or turn.status in TERMINAL_STATUSES
                or turn.cancel_requested_at
                or turn.current_command_id != saved.origin_command_id
            ):
                raise InteractionConflict("interaction is no longer answerable")
            if conversation_id is not None:
                collision = session.scalar(
                    select(InteractionRow.interaction_id)
                    .join(ConversationTurnRow)
                    .where(
                        ConversationTurnRow.conversation_id == conversation_id,
                        InteractionRow.response_key == idempotency_key,
                    )
                )
                original = session.scalar(
                    select(ConversationTurnRow.turn_id).where(
                        ConversationTurnRow.conversation_id == conversation_id,
                        ConversationTurnRow.idempotency_key == idempotency_key,
                    )
                )
                if collision or original:
                    raise InteractionConflict("channel message already belongs to another request")
            profile = self.service.releases.verify(turn.release_snapshot)
            effective = intersect_scopes(turn.release_snapshot["context"]["scopes"], scopes)
            effective = intersect_scopes(turn.grant_scopes, effective)
            require_scopes(effective, profile.required_scopes)
            if point.required_scope:
                require_scopes(scopes, {point.required_scope})
            check_authorization(turn, scopes=effective)
            evidence, expires = bounded_authorization(
                self.service.settings,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                evidence=authorization,
            )
            turn.grant_scopes = sorted(effective)
            turn.grant_expires_at = min(aware(turn.grant_expires_at), expires)
            turn.grant_source, turn.grant_source_hash = evidence.source, evidence.source_hash
            turn.grant_issued_at = evidence.issued_at
            turn.grant_revision += 1
            record_grant(session, turn)
            command_id = str(uuid4())
            previous = session.get(TurnCommandRow, saved.origin_command_id)
            payload = {
                "predecessor": previous.command_id,
                "interaction_id": interaction_id,
                "binding": request["binding"],
                "expires_at": request["expires_at"],
                "response": native_response(request, normalized),
            }
            session.add(
                TurnCommandRow(
                    command_id=command_id,
                    turn_id=turn.turn_id,
                    sequence=previous.sequence + 1,
                    kind="resume",
                    request_payload=payload,
                    request_hash=digest(payload),
                    grant_revision=turn.grant_revision,
                    authorized_scopes=sorted(effective),
                )
            )
            saved.response, saved.response_hash, saved.response_key = (
                body,
                fingerprint,
                idempotency_key,
            )
            saved.decided_by, saved.decided_at = subject_id, now()
            saved.resume_command_id = command_id
            saved.status = "rejected" if response.decision == "reject" else "resolved"
            turn.current_command_id = command_id
            if response.decision == "reject":
                turn.side_effects_denied = True
            self.service.store.transition(session, turn, "resuming", changed=True)
            return public_interaction(saved)
