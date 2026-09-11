"""Bounded command work, independent native joins and durable fallback reconciliation."""

import asyncio
import logging
from uuid import uuid4

from sqlalchemy import update

from financeclaw.api.application.turns.commands import CommandService
from financeclaw.api.application.turns.results import ResultService
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, StaleTurnLease, now

LOGGER = logging.getLogger(__name__)


class TurnLifecycle:
    """Supervise bounded command submission and independent native result observation."""

    def __init__(self, service, native):
        """Inject dependencies without starting background work."""
        self.service, self.native, self.settings = service, native, service.settings
        self.commands, self.results = CommandService(service, native), ResultService(service)
        self.owner = f"api-{uuid4()}"
        self._wake = asyncio.Event()
        self._tasks = []
        self._joins = {}
        self._command_slots = asyncio.Semaphore(self.settings.turn_command_slots)
        service.lifecycle = self

    async def start(self):
        """Start bounded scanners; graph execution remains owned by native workers."""
        self._tasks = [
            asyncio.create_task(self._scan(), name=f"turn-scanner-{i}")
            for i in range(self.settings.turn_scanner_slots)
        ]

    def wake(self):
        """Prompt local observers; the durable due timestamp remains authoritative."""
        self._wake.set()

    async def healthy(self):
        """Check that the role still owns live scanner tasks."""
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def stop(self):
        """Drain local tasks without cancelling native runs or dropping durable responsibility."""
        tasks = [*self._tasks, *self._joins.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._joins.clear()

    async def _renew(self, lease):
        """Extend only an unexpired lease held by this API epoch."""
        while True:
            await asyncio.sleep(self.settings.turn_renew_seconds)
            if not await run_sync(
                self.service.store.renew, lease, seconds=self.settings.turn_lease_seconds
            ):
                return

    async def _scan(self):
        """Keep the durable scanner alive across transient database outages."""
        while True:
            try:
                await self._scan_once()
            except Exception as exc:
                LOGGER.warning("Turn scanner retry", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(1)

    async def _scan_once(self):
        """Own at most one bounded reconciliation and release it after short transactions."""
        claim = asyncio.create_task(
            run_sync(
                self.service.store.claim_due, self.owner, seconds=self.settings.turn_lease_seconds
            )
        )
        try:
            lease = await asyncio.shield(claim)
        except asyncio.CancelledError:
            lease = await claim
            if lease:
                await run_sync(self.service.store.release, lease, delay=0)
            raise
        if lease is None:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except TimeoutError:
                pass
            return
        renewal = asyncio.create_task(self._renew(lease))
        error = None
        try:
            async with asyncio.timeout(self.settings.native_timeout_seconds):
                await self._step(lease)
        except StaleTurnLease:
            pass
        except Exception as exc:
            error = type(exc).__name__
            LOGGER.warning(
                "Turn reconciliation deferred",
                extra={"turn_id": lease.turn_id, "error_type": error},
            )
            try:
                await run_sync(self.results.blocked, lease, "reconciliation_required")
            except StaleTurnLease:
                pass
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            try:
                await run_sync(
                    self.service.store.release,
                    lease,
                    delay=self.settings.turn_fallback_seconds,
                    error=error,
                )
            except StaleTurnLease:
                pass

    async def _step(self, lease):
        """Reconcile the current command, cancellation intent and exact native observation."""
        turn, command = await run_sync(self.commands.read, lease)
        if turn["status"] in TERMINAL_STATUSES:
            return
        if turn["cancel_requested_at"] and command["state"] in {"prepared", "cancelled"}:
            await run_sync(self.results.cancelled, lease, command["command_id"])
            return
        if command["state"] in {"prepared", "sending", "uncertain"}:
            async with self._command_slots:
                await self.commands.reconcile(lease, turn, command)
            turn, command = await run_sync(self.commands.read, lease)
        if not command["native_run_id"]:
            return
        if turn["cancel_requested_at"]:
            await self.native.cancel(turn, command)
            receipt = await self.native.get(turn, command)
            if receipt["status"] not in {"pending", "running"}:
                await run_sync(self.results.cancelled, lease, command["command_id"])
            return
        if command["state"] == "observed":
            await run_sync(self.results.expire_interaction, lease)
            return
        observation = await self.native.observe(turn, command)
        await run_sync(self.results.apply, lease, command["command_id"], observation)
        if observation["status"] in {"queued", "running"}:
            self._watch(turn, command)

    def _watch(self, turn, command):
        """Reserve a bounded join slot independently of command submission capacity."""
        key = command["command_id"]
        if key in self._joins or len(self._joins) >= self.settings.turn_join_slots:
            return
        self._joins[key] = asyncio.create_task(self._join(turn, command), name=f"native-join-{key}")

    async def _join(self, turn, command):
        """Convert a native terminal hint or timeout into a durable reconciliation wakeup."""
        try:
            # Join waits for a native fact without holding a Turn lease or executing a graph.
            async with asyncio.timeout(self.settings.turn_join_seconds):
                await self.native.join(turn, command)
        except Exception as exc:
            if not isinstance(exc, TimeoutError):
                LOGGER.info(
                    "Native join reconnect deferred", extra={"error_type": type(exc).__name__}
                )
        finally:
            self._joins.pop(command["command_id"], None)

            def due():
                """Wake only the command that the join originally observed."""
                with self.service.sessions.begin() as session:
                    session.execute(
                        update(ConversationTurnRow)
                        .where(
                            ConversationTurnRow.turn_id == turn["turn_id"],
                            ConversationTurnRow.current_command_id == command["command_id"],
                            ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                        )
                        .values(next_action_at=now())
                    )

            try:
                await run_sync(due)
            except Exception as exc:
                LOGGER.warning(
                    "Native join wake deferred to scanner", extra={"error_type": type(exc).__name__}
                )
            self.wake()
