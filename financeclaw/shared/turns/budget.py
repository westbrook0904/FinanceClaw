"""Atomic Turn budgets and runtime identity validation; this module never submits runs."""

from collections.abc import Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.turns.authorization import check_authorization, intersect_scopes
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow, TurnCommandRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, ExecutionConflict, export


def snapshot_context(snapshot: Mapping, scopes=None, *, command_id=None) -> ExecutionContext:
    """Rebuild only the frozen identity; the current command supplies its own identity."""
    if not isinstance(snapshot.get("context"), Mapping):
        raise ExecutionConflict("execution authorization snapshot is missing")
    context = dict(snapshot["context"])
    if scopes is not None:
        context["scopes"] = intersect_scopes(context["scopes"], scopes)
    if command_id is not None:
        context["command_id"] = command_id
    return ExecutionContext.model_validate(context)


class TurnExecutionRepository:
    """Give all subgraphs the same budget and cancellation/authorization boundary."""

    def __init__(self, sessions: sessionmaker[Session]):
        """Inject dependencies without starting background work."""
        self.sessions = sessions

    def get(self, turn_id: str) -> dict:
        """Copy business facts without retaining an ORM session in execution code."""
        with self.sessions() as session:
            turn = session.get(ConversationTurnRow, turn_id)
            if turn is None:
                raise ExecutionConflict("execution Turn is missing")
            return export(turn)

    def verify_context(self, context: ExecutionContext) -> None:
        """Verify immutable identity, current command and the live finite grant."""
        with self.sessions() as session:
            turn = session.get(ConversationTurnRow, context.turn_id)
            if turn is None:
                raise ExecutionConflict("execution Turn is missing")
            original = snapshot_context(turn.release_snapshot, command_id=context.command_id)
            if original.model_dump(exclude={"scopes"}) != context.model_dump(exclude={"scopes"}):
                raise ExecutionConflict("runtime identity differs from the accepted Turn")
            if "*" not in original.scopes and not context.scopes.issubset(original.scopes):
                raise ExecutionConflict("runtime scopes exceed the immutable upper bound")
            command = session.get(TurnCommandRow, context.command_id)
            if (
                command is None
                or command.turn_id != turn.turn_id
                or command.command_id != turn.current_command_id
                or command.state not in {"sending", "uncertain", "submitted"}
            ):
                raise ExecutionConflict("runtime command is not the current accepted command")
            if "*" not in command.authorized_scopes and not context.scopes.issubset(
                command.authorized_scopes
            ):
                raise ExecutionConflict("runtime scopes exceed the command grant")
            check_authorization(turn, scopes=context.scopes)

    def rejected_invocation(self, context, tool_call_id, *, invocation_id=None) -> bool:
        """Allow only the current rejection command to re-enter its interrupted composite tool."""
        with self.sessions() as session:
            decision = session.scalar(
                select(InteractionRow).where(
                    InteractionRow.turn_id == context.turn_id,
                    InteractionRow.resume_command_id == context.command_id,
                    InteractionRow.status == "rejected",
                )
            )
            invocation = decision.request.get("invocation") if decision else None
            return bool(
                invocation
                and invocation.get("root_tool_call_id") == tool_call_id
                and (invocation_id is None or invocation.get("invocation_id") == invocation_id)
            )

    def consume(self, turn_id: str, kind: str, *, session: Session | None = None) -> None:
        """Reserve one real attempt atomically; retries and resumes never reset counts."""
        if session is None:
            with self.sessions.begin() as transaction:
                self.consume(turn_id, kind, session=transaction)
            return
        turn = session.scalar(
            select(ConversationTurnRow)
            .where(ConversationTurnRow.turn_id == turn_id)
            .with_for_update()
        )
        if turn is None:
            raise ExecutionConflict("execution budget is missing")
        check_authorization(turn)
        column = {
            "model": ConversationTurnRow.model_calls,
            "tool": ConversationTurnRow.tool_calls,
            "command": ConversationTurnRow.command_calls,
        }[kind]
        limit = turn.release_snapshot["limits"][kind]
        changed = session.execute(
            update(ConversationTurnRow)
            .where(
                ConversationTurnRow.turn_id == turn_id,
                ConversationTurnRow.cancel_requested_at.is_(None),
                ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                column < limit,
            )
            .values({column.key: column + 1}),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount != 1:
            raise ExecutionConflict(f"Turn {kind} budget exhausted or cancellation requested")
        session.expire(turn, [column.key])

    def deny_side_effects(self, turn_id: str) -> None:
        """Persist rejection before the graph continues to any later write-capable tool."""
        with self.sessions.begin() as session:
            changed = session.execute(
                update(ConversationTurnRow)
                .where(
                    ConversationTurnRow.turn_id == turn_id,
                )
                .values(side_effects_denied=True)
            )
            if changed.rowcount != 1:
                raise ExecutionConflict("execution Turn is missing")
