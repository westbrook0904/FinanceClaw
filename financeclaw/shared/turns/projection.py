"""Safe product projections shared by HTTP streams and channel notifications."""

from sqlalchemy import select
from sqlalchemy.orm import load_only

from financeclaw.kernel.responses import TurnSnapshot
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.infrastructure.security.redaction import redact_sensitive
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow
from financeclaw.shared.turns.types import aware

# The immutable release can be large; reading a product snapshot never needs it.
SNAPSHOT_COLUMNS = load_only(
    ConversationTurnRow.turn_id,
    ConversationTurnRow.conversation_id,
    ConversationTurnRow.status,
    ConversationTurnRow.revision,
    ConversationTurnRow.status_reason,
    ConversationTurnRow.grant_revision,
    raiseload=True,
)


def public_interaction(row):
    """Expose the governed question, never checkpoints, transport IDs or submitted answers."""
    request, point = row.request, row.request["point"]
    result = {
        "interaction_id": row.interaction_id,
        "turn_id": row.turn_id,
        "revision": row.revision,
        "kind": row.kind,
        "status": row.status,
        "question": request["question"],
        "expires_at": aware(row.expires_at).isoformat(),
        "response_url": f"/v1/interactions/{row.interaction_id}/responses",
    }
    if point["kind"] == "input":
        result["response_schema"] = point["response_schema"]
    elif point["kind"] == "choice":
        result.update(
            {
                key: point[key]
                for key in ("options", "selection_mode", "min_selected", "max_selected")
            }
        )
    else:
        result.update(
            action_hash=request["action_hash"],
            allowed_decisions=request["allowed_decisions"],
            action=redact_sensitive(request["action"]),
        )
    return result


def snapshot(session, turn):
    """Read one safe view while the caller holds the Turn's shared lock."""
    return snapshots(session, [turn])[turn.turn_id]


def snapshots(session, turns):
    """Fetch interactions and final answers once per batch, regardless of subscriber count."""
    identifiers = [turn.turn_id for turn in turns]
    if not identifiers:
        return {}
    pending = {}
    for row in session.scalars(
        select(InteractionRow).where(
            InteractionRow.turn_id.in_(identifiers), InteractionRow.status == "pending"
        )
    ):
        pending.setdefault(row.turn_id, []).append(public_interaction(row))
    completed = [turn.turn_id for turn in turns if turn.status == "completed"]
    answers = (
        dict(
            session.execute(
                select(ConversationMessageRow.turn_id, ConversationMessageRow.content).where(
                    ConversationMessageRow.turn_id.in_(completed),
                    ConversationMessageRow.role == "assistant",
                    ConversationMessageRow.parent_message_id.is_(None),
                )
            ).all()
        )
        if completed
        else {}
    )
    return {
        turn.turn_id: TurnSnapshot(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            status=turn.status,
            revision=turn.revision,
            reason=turn.status_reason,
            pending_interactions=tuple(pending.get(turn.turn_id, ())),
            output={"messages": [{"type": "assistant", "content": answers[turn.turn_id]}]}
            if turn.turn_id in answers
            else None,
            authorization_revision=turn.grant_revision,
        )
        for turn in turns
    }
