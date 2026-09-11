"""Short transactions for business ownership, bounded leases and product revisions."""

from datetime import timedelta

from sqlalchemy import inspect, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import (
    TERMINAL_STATUSES,
    StaleTurnLease,
    TurnLease,
    TurnNotFound,
    aware,
    export,
    now,
)


class TurnRepository:
    """Lease product responsibility without implementing a second graph execution queue."""

    def __init__(self, sessions: sessionmaker[Session]):
        """Inject dependencies without starting background work."""
        self.sessions = sessions

    def require_schema(self) -> None:
        """Fail readiness on an uninitialized or incompatible application database."""
        from financeclaw.shared.turns.tables import InteractionRow, TurnCommandRow

        with self.sessions() as session:
            schema = inspect(session.get_bind())
            for table in (ConversationTurnRow, TurnCommandRow, InteractionRow):
                if not schema.has_table(table.__tablename__) or not set(
                    table.__table__.columns.keys()
                ).issubset(column["name"] for column in schema.get_columns(table.__tablename__)):
                    raise RuntimeError("Stage 10 application schema is required")

    def responsibility_healthy(self, *, overdue_seconds: float = 300) -> bool:
        """Detect overdue ownership without penalizing native execution or human wait time."""
        at = now()
        with self.sessions() as session:
            overdue = session.scalar(
                select(ConversationTurnRow.turn_id)
                .where(
                    ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                    ConversationTurnRow.next_action_at < at - timedelta(seconds=overdue_seconds),
                    or_(
                        ConversationTurnRow.lease_until.is_(None),
                        ConversationTurnRow.lease_until <= at,
                    ),
                )
                .limit(1)
            )
            return overdue is None

    def assert_owned(self, turn_id, tenant_id, subject_id, *, conversation_id=None) -> None:
        """Check access using indexed identity columns without loading the frozen release."""
        with self.sessions() as session:
            query = select(ConversationTurnRow.turn_id).where(
                ConversationTurnRow.turn_id == turn_id,
                ConversationTurnRow.tenant_id == tenant_id,
                ConversationTurnRow.subject_id == subject_id,
            )
            if conversation_id is not None:
                query = query.where(ConversationTurnRow.conversation_id == conversation_id)
            if session.scalar(query) is None:
                raise TurnNotFound("Turn not found")

    def owned(
        self, turn_id: str, tenant_id: str, subject_id: str, *, conversation_id: str | None = None
    ) -> dict:
        """Return the same absence error for unknown and foreign Turns."""
        with self.sessions() as session:
            turn = session.scalar(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.turn_id == turn_id,
                    ConversationTurnRow.tenant_id == tenant_id,
                    ConversationTurnRow.subject_id == subject_id,
                )
            )
            if (
                turn is None
                or conversation_id is not None
                and turn.conversation_id != conversation_id
            ):
                raise TurnNotFound("Turn not found")
            return export(turn)

    @staticmethod
    def lock(session: Session, turn_id: str, lease: TurnLease | None = None) -> ConversationTurnRow:
        """Serialize decisions with budget/observation writes on both supported SQL dialects."""
        session.execute(
            update(ConversationTurnRow)
            .where(
                ConversationTurnRow.turn_id == turn_id,
            )
            .values(lease_epoch=ConversationTurnRow.lease_epoch)
        )
        turn = session.get(ConversationTurnRow, turn_id, populate_existing=True)
        if turn is None:
            raise TurnNotFound("Turn not found")
        if lease is not None and (
            turn.lease_owner != lease.owner
            or turn.lease_epoch != lease.epoch
            or turn.lease_until is None
            or aware(turn.lease_until) <= now()
        ):
            raise StaleTurnLease("Turn lease is no longer current")
        return turn

    def claim_due(self, owner: str, *, seconds: int = 60) -> TurnLease | None:
        """Claim one due responsibility in database order, skipping another API's rows."""
        with self.sessions.begin() as session:
            at = now()
            turn = session.scalar(
                select(ConversationTurnRow)
                .where(
                    ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                    ConversationTurnRow.next_action_at <= at,
                    or_(
                        ConversationTurnRow.lease_until.is_(None),
                        ConversationTurnRow.lease_until <= at,
                    ),
                )
                .order_by(ConversationTurnRow.next_action_at, ConversationTurnRow.turn_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if turn is None:
                return None
            epoch = turn.lease_epoch + 1
            changed = session.execute(
                update(ConversationTurnRow)
                .where(
                    ConversationTurnRow.turn_id == turn.turn_id,
                    ConversationTurnRow.lease_epoch == turn.lease_epoch,
                    or_(
                        ConversationTurnRow.lease_until.is_(None),
                        ConversationTurnRow.lease_until <= at,
                    ),
                )
                .values(
                    lease_owner=owner,
                    lease_epoch=epoch,
                    lease_until=at + timedelta(seconds=seconds),
                    next_action_at=at + timedelta(seconds=seconds),
                ),
                execution_options={"synchronize_session": False},
            )
            return (
                TurnLease(turn.turn_id, owner, epoch, turn.revision) if changed.rowcount else None
            )

    def renew(self, lease: TurnLease, *, seconds: int = 60) -> bool:
        """Reject late renewal so an expired holder cannot reacquire its previous epoch."""
        with self.sessions.begin() as session:
            changed = session.execute(
                update(ConversationTurnRow)
                .where(
                    ConversationTurnRow.turn_id == lease.turn_id,
                    ConversationTurnRow.lease_owner == lease.owner,
                    ConversationTurnRow.lease_epoch == lease.epoch,
                    ConversationTurnRow.lease_until > now(),
                )
                .values(lease_until=now() + timedelta(seconds=seconds))
            )
            return changed.rowcount == 1

    def release(self, lease: TurnLease, *, delay: float, error: str | None = None) -> None:
        """Release without overwriting a newer answer/cancel wakeup or product revision."""
        with self.sessions.begin() as session:
            turn = self.lock(session, lease.turn_id, lease)
            if turn.revision == lease.revision:
                turn.next_action_at = min(
                    aware(turn.next_action_at), now() + timedelta(seconds=delay)
                )
            turn.lease_owner = turn.lease_until = None
            turn.last_error_code = error

    @staticmethod
    def wake(session: Session, turn: ConversationTurnRow) -> None:
        """Postgres delivers notifications after commit; the row remains the work source."""
        turn.next_action_at = now()
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_notify('financeclaw_turns', :payload)"),
                {"payload": f"{turn.turn_id}:{turn.revision}"},
            )

    def transition(
        self,
        session: Session,
        turn: ConversationTurnRow,
        status: str,
        reason: str | None = None,
        *,
        changed: bool = False,
    ) -> None:
        """Commit one safe state revision and its durable notification intent together."""
        if not changed and (turn.status, turn.status_reason) == (status, reason):
            return
        turn.status, turn.status_reason = status, reason
        turn.revision += 1
        turn.updated_at = now()
        if status in TERMINAL_STATUSES:
            turn.finished_at = now()
        self.wake(session, turn)
        session.flush()
        from financeclaw.shared.notifications.facts import record_progress

        record_progress(session, turn)
        from financeclaw.shared.turns.audit import record_fact

        record_fact(
            session,
            turn,
            "state",
            turn.revision,
            {"status": status, "reason": reason, "command_id": turn.current_command_id},
        )
