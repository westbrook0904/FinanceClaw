"""Apply fenced native observations to the single product truth transaction."""

from datetime import datetime

from sqlalchemy import func, select, update

from financeclaw.api.application.turns.backend import interrupts
from financeclaw.api.application.turns.waits import map_wait
from financeclaw.shared.turns.tables import InteractionRow, TurnCommandRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, ExecutionConflict, aware, digest, now


class ResultService:
    """Commit verified native observations and product delivery facts atomically."""

    def __init__(self, service):
        """Inject dependencies without starting background work."""
        self.service = service

    def blocked(self, lease, reason):
        """Persist a recoverable observation failure without claiming terminal completion."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            command = session.get(TurnCommandRow, turn.current_command_id)
            if command.state in {"sending", "uncertain"}:
                reason = "submission_uncertain"
            elif turn.grant_revoked or aware(turn.grant_expires_at) <= now():
                reason = "authorization_required"
            if turn.status not in TERMINAL_STATUSES:
                self.service.store.transition(
                    session, turn, "cancelling" if turn.cancel_requested_at else "blocked", reason
                )

    def cancelled(self, lease, command_id):
        """Call only after no command was sent or the exact native attempt has stopped."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            if turn.current_command_id != command_id or not turn.cancel_requested_at:
                raise ExecutionConflict("cancellation refers to a stale command")
            command = session.get(TurnCommandRow, command_id)
            if command.state in {"sending", "uncertain"}:
                raise ExecutionConflict("cannot confirm an unknown native attempt stopped")
            command.state = "cancelled" if command.native_run_id is None else "observed"
            session.execute(
                update(InteractionRow)
                .where(InteractionRow.turn_id == turn.turn_id, InteractionRow.status == "pending")
                .values(status="cancelled", decided_at=now())
            )
            self.service.store.transition(session, turn, "cancelled")

    def apply(self, lease, command_id, observation):
        """Journal, final state, history intent and notifications commit together."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            if turn.status in TERMINAL_STATUSES or turn.cancel_requested_at:
                return
            if turn.current_command_id != command_id:
                return  # A new answer already made the predecessor obsolete.
            command = session.get(TurnCommandRow, command_id)
            status = observation["status"]
            if status in {"queued", "running"}:
                self.service.store.transition(session, turn, status)
                return
            if status == "waiting":
                state = observation["state"]
                interrupt = interrupts(state)[0]
                identifier = "interaction-" + digest([turn.turn_id, command_id, interrupt["id"]])
                prior = session.get(InteractionRow, identifier)
                if prior:
                    if prior.checkpoint != state["checkpoint"] or prior.request["binding"][
                        "interrupt_hash"
                    ] != digest(interrupt["value"]):
                        raise ExecutionConflict("native wait changed without a new command")
                    return
                request = map_wait(
                    observation, turn.release_snapshot, self.service.releases, self.service.settings
                )
                revision = (
                    session.scalar(
                        select(func.max(InteractionRow.revision)).where(
                            InteractionRow.turn_id == turn.turn_id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    InteractionRow(
                        interaction_id=identifier,
                        turn_id=turn.turn_id,
                        origin_command_id=command_id,
                        native_run_id=observation["native_run_id"],
                        checkpoint=state["checkpoint"],
                        interrupt_id=interrupt["id"],
                        revision=revision,
                        kind=request["point"]["kind"],
                        request=request,
                        request_hash=digest(request),
                        expires_at=datetime.fromisoformat(request["expires_at"]),
                    )
                )
                command.state = "observed"
                command.observation_checkpoint = state["checkpoint"]
                command.observation_hash = digest(request["binding"])
                self.service.store.transition(session, turn, "waiting", "user_interaction")
                return
            if status not in {"completed", "failed"}:
                raise ExecutionConflict("unsupported product observation")
            command.state = "observed"
            command.observation_checkpoint = observation.get("checkpoint")
            command.observation_hash = digest(observation)
            if status == "completed":
                self.service.journal.append_assistant_message(
                    turn_id=turn.turn_id, content=observation["content"], session=session
                )
            session.execute(
                update(InteractionRow)
                .where(InteractionRow.turn_id == turn.turn_id, InteractionRow.status == "pending")
                .values(status="superseded", decided_at=now())
            )
            self.service.store.transition(session, turn, status, observation.get("reason"))

    def expire_interaction(self, lease):
        """Close expired questions and publish their blocked product state."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            changed = session.execute(
                update(InteractionRow)
                .where(
                    InteractionRow.turn_id == turn.turn_id,
                    InteractionRow.status == "pending",
                    InteractionRow.expires_at <= now(),
                )
                .values(status="expired", decided_at=now())
            )
            if changed.rowcount:
                self.service.store.transition(session, turn, "blocked", "interaction_expired")
