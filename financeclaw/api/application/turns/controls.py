"""Human cancellation and finite authorization, serialized with decisions and observations."""

from sqlalchemy import select, update

from financeclaw.shared.turns.audit import record_grant
from financeclaw.shared.turns.authorization import bounded_authorization, intersect_scopes
from financeclaw.shared.turns.tables import InteractionRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, ExecutionConflict, now


def apply_control(service, session, turn, op, *, scopes=(), authorization=None, revision=None):
    """Apply a user control while the caller holds the Turn lock and audit transaction."""
    if op == "cancel":
        if turn.status not in TERMINAL_STATUSES and not turn.cancel_requested_at:
            turn.cancel_requested_at = now()
            session.execute(
                update(InteractionRow)
                .where(InteractionRow.turn_id == turn.turn_id, InteractionRow.status == "pending")
                .values(status="cancelled", decided_at=now())
            )
            service.store.transition(session, turn, "cancelling", "execution_stop_not_confirmed")
        return
    if op not in {"authorize", "revoke"}:
        raise ExecutionConflict("unsupported Turn control")
    if revision is None or turn.grant_revision != revision:
        raise ExecutionConflict("authorization view is stale")
    if turn.status in TERMINAL_STATUSES or turn.cancel_requested_at:
        raise ExecutionConflict("Turn cannot change authorization")
    if op == "authorize":
        evidence, expires = bounded_authorization(
            service.settings,
            tenant_id=turn.tenant_id,
            subject_id=turn.subject_id,
            scopes=scopes,
            evidence=authorization,
        )
        turn.grant_scopes = sorted(
            intersect_scopes(turn.release_snapshot["context"]["scopes"], scopes)
        )
        turn.grant_source, turn.grant_source_hash = evidence.source, evidence.source_hash
        turn.grant_issued_at, turn.grant_expires_at, turn.grant_revoked = (
            evidence.issued_at,
            expires,
            False,
        )
    else:
        turn.grant_revoked = True
    turn.grant_revision += 1
    record_grant(session, turn)
    pending = session.scalar(
        select(InteractionRow).where(
            InteractionRow.turn_id == turn.turn_id, InteractionRow.status == "pending"
        )
    )
    if op == "authorize" and pending and pending.expires_at:
        from financeclaw.shared.turns.types import aware

        if aware(pending.expires_at) > now():
            service.store.transition(session, turn, "waiting", "user_interaction", changed=True)
            return
    service.store.transition(
        session,
        turn,
        "blocked",
        "authorization_updated" if op == "authorize" else "authorization_required",
        changed=True,
    )
