"""Product facade. Admission and controls never synchronously wait for native execution."""

from financeclaw.api.application.turns.admission import TurnAdmission
from financeclaw.api.application.turns.controls import apply_control
from financeclaw.api.application.turns.interactions import TurnInteractions
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.budget import TurnExecutionRepository
from financeclaw.shared.turns.projection import SNAPSHOT_COLUMNS, snapshot
from financeclaw.shared.turns.receipts import read_receipt, save_receipt
from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import ExecutionConflict, digest


class TurnService:
    """Coordinate product admission, decisions and read-only views over one shared Turn model."""

    def __init__(self, store, journal, releases, settings):
        """Inject dependencies without starting background work."""
        self.store, self.sessions = store, store.sessions
        self.journal, self.releases, self.settings = journal, releases, settings
        self.execution = TurnExecutionRepository(store.sessions)
        self.admission, self.interactions = TurnAdmission(self), TurnInteractions(self)
        self.lifecycle = None
        self.events = None

    def wake(self):
        """Prompt local observers; the durable due timestamp remains authoritative."""
        if self.lifecycle:
            self.lifecycle.wake()

    async def start_turn(self, conversation_id, request, **kwargs):
        """Persist one input and immutable command before notifying background observers."""
        result = await run_sync(self.admission.accept, conversation_id, request, **kwargs)
        self.wake()
        return result

    def assert_owned(self, turn_id, *, tenant_id, subject_id):
        """Reject foreign and missing Turns using the same public absence semantics."""
        return self.store.assert_owned(turn_id, tenant_id, subject_id)

    def read_snapshot(self, turn_id, *, tenant_id, subject_id):
        """Read status, interactions and Journal under a consistent shared Turn lock."""
        self.assert_owned(turn_id, tenant_id=tenant_id, subject_id=subject_id)
        with self.sessions.begin() as session:
            # A short shared lock keeps status, interactions and final Journal at one revision.
            from sqlalchemy import select

            turn = session.scalar(
                select(ConversationTurnRow)
                .options(SNAPSHOT_COLUMNS)
                .where(ConversationTurnRow.turn_id == turn_id)
                .with_for_update(read=True)
            )
            return snapshot(session, turn)

    async def status(self, turn_id, *, tenant_id, subject_id, scopes=()):
        """Read the current product snapshot without advancing execution."""
        return await run_sync(
            self.read_snapshot, turn_id, tenant_id=tenant_id, subject_id=subject_id
        )

    async def assistant_content(self, turn_id, **kwargs):
        """Return the final public Journal answer when the Turn has completed."""
        value = await self.status(turn_id, **kwargs)
        return value.output["messages"][0]["content"] if value.output else None

    async def control(
        self,
        turn_id,
        op,
        *,
        tenant_id,
        subject_id,
        scopes=(),
        authorization=None,
        command_id=None,
        expected_grant_revision=None,
    ):
        """Serialize idempotent user controls with decisions and native result observations."""
        if not command_id or not command_id.strip() or len(command_id) > 256:
            raise ExecutionConflict("control idempotency key is required")
        self.assert_owned(turn_id, tenant_id=tenant_id, subject_id=subject_id)
        fingerprint = digest([op, sorted(scopes), expected_grant_revision])

        def accept():
            """Commit a new control or reuse its immutable audit receipt."""
            with self.sessions.begin() as session:
                turn = self.store.lock(session, turn_id)
                if read_receipt(session, turn, command_id, fingerprint) is not None:
                    return
                apply_control(
                    self,
                    session,
                    turn,
                    op,
                    scopes=scopes,
                    authorization=authorization,
                    revision=turn.grant_revision
                    if expected_grant_revision is None
                    else expected_grant_revision,
                )
                save_receipt(session, turn, command_id, fingerprint, {"revision": turn.revision})

        await run_sync(accept)
        self.wake()
        return await self.status(turn_id, tenant_id=tenant_id, subject_id=subject_id)

    async def cancel(self, turn_id, **kwargs):
        """Persist cancellation intent; observation confirms when execution has stopped."""
        return await self.control(turn_id, "cancel", **kwargs)

    async def reauthorize(self, turn_id, **kwargs):
        """Record a finite grant bounded by the original accepted capabilities."""
        return await self.control(turn_id, "authorize", **kwargs)

    async def revoke_authorization(self, turn_id, **kwargs):
        """Persist grant revocation without hiding previously completed results."""
        return await self.control(turn_id, "revoke", **kwargs)

    async def stream(self, turn_id, *, tenant_id, subject_id, scopes=(), last_event_id=None):
        """Yield latest product revisions and heartbeats, with no execution side effects."""
        async for event in self.events.stream(turn_id, tenant_id, subject_id, last_event_id):
            yield event

    def notifications(self, turn_id, *, tenant_id, subject_id, revoke=False):
        """Read or revoke the fixed delivery subscription of the owned Turn."""
        from sqlalchemy import select

        from financeclaw.shared.notifications.tables import NotificationTargetRow

        self.assert_owned(turn_id, tenant_id=tenant_id, subject_id=subject_id)
        with self.sessions.begin() as session:
            target = session.scalar(
                select(NotificationTargetRow)
                .where(NotificationTargetRow.turn_id == turn_id)
                .with_for_update()
            )
            if target is None:
                return {"turn_id": turn_id, "subscribed": False}
            if revoke:
                target.active = False
            return {"turn_id": turn_id, "subscribed": True, "active": target.active}
