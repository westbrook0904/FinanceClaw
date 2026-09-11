"""Snapshot SSE fan-out with one PostgreSQL listener and one batched recovery reader."""

import asyncio
import logging
from collections import defaultdict

from sqlalchemy import select

from financeclaw.kernel.responses import StreamEvent
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.projection import SNAPSHOT_COLUMNS, snapshots
from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES

LOGGER = logging.getLogger(__name__)


class TurnEvents:
    """Fan out bounded latest snapshots through one listener and shared batch refresh."""

    def __init__(self, service, *, refresh_seconds=5, heartbeat_seconds=15):
        """Inject dependencies without starting background work."""
        self.service = service
        self.refresh_seconds, self.heartbeat_seconds = refresh_seconds, heartbeat_seconds
        self._subscribers = defaultdict(set)
        self._tasks = []
        self._changed = asyncio.Event()
        service.events = self

    async def start(self):
        """Start one shared refresher and, on PostgreSQL, one LISTEN connection."""
        self._tasks = [asyncio.create_task(self._refresh())]
        with self.service.sessions() as session:
            url = session.get_bind().url
        if url.get_backend_name() == "postgresql":
            dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
            self._tasks.append(asyncio.create_task(self._listen(dsn)))

    async def healthy(self):
        """Report whether both refresh and listener responsibilities remain supervised."""
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def stop(self):
        """Drain local tasks without cancelling native runs or dropping durable responsibility."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _listen(self, dsn):
        """Treat PostgreSQL notifications as wake hints and refresh after reconnection."""
        import psycopg

        while True:
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                    await conn.execute("LISTEN financeclaw_turns")
                    self._changed.set()  # Reconnection requires a full subscribed snapshot refresh.
                    async for _notification in conn.notifies():
                        self._changed.set()
                        self.service.wake()
            except Exception:
                await asyncio.sleep(1)

    def _batch(self, identifiers):
        """Read subscribed Turn projections in one transaction with a constant query count."""
        with self.service.sessions.begin() as session:
            turns = list(
                session.scalars(
                    select(ConversationTurnRow)
                    .options(SNAPSHOT_COLUMNS)
                    .where(ConversationTurnRow.turn_id.in_(identifiers))
                    .with_for_update(read=True)
                )
            )
            return snapshots(session, turns)

    async def _refresh(self):
        """Coalesce subscriber wakeups and recover snapshots after missed notifications."""
        while True:
            self._changed.clear()
            if self._subscribers:
                try:
                    values = await run_sync(self._batch, tuple(self._subscribers))
                except Exception as exc:
                    LOGGER.warning(
                        "Snapshot refresh deferred", extra={"error_type": type(exc).__name__}
                    )
                    values = {}
                for key, value in values.items():
                    for queue in tuple(self._subscribers.get(key, ())):
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(value)
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=self.refresh_seconds)
            except TimeoutError:
                pass

    async def stream(self, turn_id, tenant_id, subject_id, last_event_id=None):
        """Yield latest product revisions and heartbeats, with no execution side effects."""
        await run_sync(
            self.service.assert_owned, turn_id, tenant_id=tenant_id, subject_id=subject_id
        )
        queue = asyncio.Queue(maxsize=1)
        # Register first; updates racing the initial snapshot remain in the local queue.
        self._subscribers[turn_id].add(queue)
        revision = -1
        try:
            value = await self.service.status(turn_id, tenant_id=tenant_id, subject_id=subject_id)
            while True:
                if value.revision > revision:
                    revision = value.revision
                    yield StreamEvent(
                        event="turn.snapshot",
                        id=f"{turn_id}:{revision}",
                        data=value.model_dump(mode="json"),
                    )
                if value.status in TERMINAL_STATUSES:
                    return
                try:
                    value = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_seconds)
                except TimeoutError:
                    yield StreamEvent(event="heartbeat", data={})
        finally:
            self._subscribers[turn_id].discard(queue)
            if not self._subscribers[turn_id]:
                del self._subscribers[turn_id]
