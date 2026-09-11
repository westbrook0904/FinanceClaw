"""Script native evidence while retaining real Stage 10 lifecycle behavior."""

from types import SimpleNamespace

from sqlalchemy import select

from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from tests.stage10.runtime import final_state, waiting_state
from tests.stage10.runtime import tick as reconcile


async def tick(setup):
    """Provide the tick boundary for this test scenario."""
    runtime = setup.runtime
    with setup.store.sessions() as session:
        roots = list(session.scalars(select(ConversationTurnRow)))
        for turn in roots:
            command = session.get(TurnCommandRow, turn.current_command_id)
            if not command.native_run_id or command.state == "observed":
                continue
            accepted = SimpleNamespace(turn_id=turn.turn_id)
            if setup.backend.questions and len(runtime.client.calls) == 1:
                waiting_state(runtime, accepted)
            else:
                final_state(runtime, accepted)
    await reconcile(runtime)
    setup.backend.calls = len(runtime.client.calls)
