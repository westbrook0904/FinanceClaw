"""Explicit synthetic admission for isolated graph tests; never used by production."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from financeclaw.shared.conversation.repository import content_hash
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import digest, now


def seed_execution(execution, context, snapshot, *, message="test input"):
    """Install a complete, constrained Turn with a current command and finite grant."""
    with execution.sessions() as session:
        prior = session.get(ConversationTurnRow, context.turn_id)
        if prior:
            return context.model_copy(
                update={
                    "conversation_id": prior.conversation_id,
                    "command_id": prior.current_command_id,
                }
            )
    context = context.model_copy(
        update={
            "conversation_id": context.conversation_id or f"test-conversation-{uuid4()}",
            "command_id": context.command_id or f"command-{context.turn_id}",
        }
    )
    with execution.sessions.begin() as session:
        conversation = session.get(ConversationRow, context.conversation_id)
        if not conversation:
            conversation = ConversationRow(
                conversation_id=context.conversation_id,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                agent_id="finance_agent",
                agent_profile_version="1.6.0",
                agent_thread_id=str(uuid4()),
            )
            session.add(conversation)
            session.flush()
        sequence = (
            session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == context.conversation_id
                )
            )
            or 0
        ) + 1
        message_id = snapshot.get("user_message_id") or f"user-{uuid4()}"
        snapshot = {
            **snapshot,
            "context": context.model_copy(update={"command_id": None}).model_dump(mode="json"),
            "user_message_id": message_id,
            "user_message_sequence": sequence,
        }
        turn = ConversationTurnRow(
            turn_id=context.turn_id,
            conversation_id=context.conversation_id,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            idempotency_key=context.turn_id,
            request_hash=digest(message),
            user_message_id=message_id,
            thread_id=snapshot.get("thread_id", conversation.agent_thread_id),
            release_snapshot=snapshot,
            release_hash=digest(snapshot),
            current_command_id=context.command_id,
            grant_scopes=sorted(context.scopes),
            grant_source="development",
            grant_source_hash="a" * 64,
            grant_issued_at=now(),
            grant_expires_at=now() + timedelta(hours=1),
            status="running",
        )
        session.add(turn)
        session.flush()
        session.add(
            ConversationMessageRow(
                message_id=message_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                sequence=sequence,
                role="user",
                content=message,
                content_hash=content_hash(message),
            )
        )
        session.add(
            TurnCommandRow(
                command_id=context.command_id,
                turn_id=context.turn_id,
                sequence=1,
                kind="start",
                request_payload={},
                request_hash=digest({}),
                grant_revision=1,
                authorized_scopes=sorted(context.scopes),
                state="submitted",
                native_run_id=str(uuid4()),
            )
        )
    return context


def finish_turn(repository, turn_id, status="completed"):
    """Set terminal test fixtures without a native receipt; runtime tests use ResultService."""
    with repository._sessions.begin() as session:
        turn = session.get(ConversationTurnRow, turn_id)
        turn.status, turn.finished_at = status, now()
        session.get(TurnCommandRow, turn.current_command_id).state = "observed"


def cancel_execution(execution, turn_id):
    """Inject cancellation at the persistent worker boundary."""
    with execution.sessions.begin() as session:
        session.get(ConversationTurnRow, turn_id).cancel_requested_at = now()
