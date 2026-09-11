"""The non-reclaimable sending right is distinct from a reclaimable Turn lease."""

from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.authorization import check_authorization, require_scopes
from financeclaw.shared.turns.tables import TurnCommandRow
from financeclaw.shared.turns.types import ExecutionConflict, export, now


class CommandService:
    """Separate a reclaimable observation lease from a non-reclaimable sending right."""

    def __init__(self, service, native):
        """Inject dependencies without starting background work."""
        self.service, self.native = service, native

    def read(self, lease):
        """Read the current immutable command under a valid Turn lease."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            command = session.get(TurnCommandRow, turn.current_command_id)
            return export(turn), export(command)

    def claim_send(self, lease, command_id):
        """Reserve command budget and the sole sending right in one short transaction."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            command = session.get(TurnCommandRow, command_id)
            if command_id != turn.current_command_id or command.state != "prepared":
                return None
            if turn.cancel_requested_at:
                command.state = "cancelled"
                return None
            check_authorization(turn, scopes=command.authorized_scopes)
            profile = self.service.releases.verify(turn.release_snapshot)
            require_scopes(command.authorized_scopes, profile.required_scopes)
            self.service.execution.consume(turn.turn_id, "command", session=session)
            command.state, command.updated_at = "sending", now()
            session.flush()
            return export(turn), export(command)

    def bind(self, lease, command_id, native_run_id):
        """Attach an exact native receipt without replacing an existing command identity."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            command = session.get(TurnCommandRow, command_id)
            if command.turn_id != turn.turn_id or command.command_id != turn.current_command_id:
                raise ExecutionConflict("receipt no longer belongs to current command")
            if command.native_run_id and command.native_run_id != native_run_id:
                raise ExecutionConflict("command already has a different receipt")
            command.native_run_id = native_run_id
            command.receipt_bound_at, command.updated_at = now(), now()
            command.state = "submitted"
            self.service.store.transition(
                session, turn, "cancelling" if turn.cancel_requested_at else "queued"
            )

    def uncertain(self, lease, command_id):
        """Preserve an unknown submission outcome instead of granting another send."""
        with self.service.sessions.begin() as session:
            turn = self.service.store.lock(session, lease.turn_id, lease)
            command = session.get(TurnCommandRow, command_id)
            if command.state == "sending":
                command.state = "uncertain"
            self.service.store.transition(
                session,
                turn,
                "cancelling" if turn.cancel_requested_at else "blocked",
                "submission_uncertain",
            )

    async def reconcile(self, lease, turn, command):
        """Send a prepared command once or exhaustively recover a previously sent receipt."""
        if command["state"] == "prepared":
            claimed = await run_sync(self.claim_send, lease, command["command_id"])
            if claimed is None:
                return
            try:
                native_run_id = await self.native.submit(*claimed)
            except Exception:
                await run_sync(self.uncertain, lease, command["command_id"])
                raise
            await run_sync(self.bind, lease, command["command_id"], native_run_id)
        elif command["state"] in {"sending", "uncertain"}:
            await run_sync(self.uncertain, lease, command["command_id"])
            native_run_id = await self.native.lookup(turn, command)
            if native_run_id:
                await run_sync(self.bind, lease, command["command_id"], native_run_id)
