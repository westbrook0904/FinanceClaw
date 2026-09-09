"""Bounded BFF lifespan loops recover database responsibility independently of clients."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from financeclaw.shared.execution_ledger.root_repository import StaleRunLease, now
from financeclaw.shared.execution_ledger.run_tables import RootRunRow


class BFFRunLifecycle:
    """In-memory tasks wake durable work; database operations and leases survive restarts."""

    def __init__(self, service, commands, observer):
        """Use a bounded number of lanes per BFF process and one owner identity per process."""
        self.service, self.store = service, service.store
        self.commands, self.observer, self.settings = commands, observer, service.settings
        self.owner = "bff-" + uuid4().hex
        self.tasks, self.signal, self.stopping = [], asyncio.Event(), False
        self.last_tick = None
        service.lifecycle = self

    def wake(self):
        """Reduce admission latency without making the signal a source of work ownership."""
        self.signal.set()

    async def start(self):
        """Validate migration before enabling request serving and persistent recovery lanes."""
        await asyncio.to_thread(self.store.require_schema)
        if self.tasks:
            return
        self.stopping = False
        self.tasks = [
            asyncio.create_task(self._lane(index), name=f"bff-runs-{index}")
            for index in range(self.settings.bff_run_concurrency)
        ]

    async def stop(self):
        """Stop local work; claimed commands retain their uncertain receipt facts on restart."""
        self.stopping = True
        self.signal.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def _renew(self, claim):
        """Renew while network calls are in flight; an expired owner cannot reacquire its epoch."""
        while True:
            await asyncio.sleep(self.settings.bff_run_lease_seconds / 3)
            renewed = await asyncio.to_thread(
                self.store.renew, claim, lease_seconds=self.settings.bff_run_lease_seconds
            )
            if not renewed:
                return

    async def tick(self, *, lane=0):
        """Claim one root and process only existing commands plus exact observation."""
        # A cancelled to_thread await does not cancel its SQL transaction. Join the claim
        # before shutdown so a freshly committed lease cannot be abandoned before finally.
        claiming = asyncio.create_task(
            asyncio.to_thread(
                self.store.claim_due,
                f"{self.owner}:{lane}",
                lease_seconds=self.settings.bff_run_lease_seconds,
            )
        )
        try:
            claim = await asyncio.shield(claiming)
        except asyncio.CancelledError:
            claim = await claiming
            if claim is not None:
                with suppress(StaleRunLease):
                    await asyncio.to_thread(self.store.finish, claim, delay=0, error="BFFShutdown")
            raise
        self.last_tick = now()
        if claim is None:
            return False
        renew = asyncio.create_task(self._renew(claim))
        error = None
        try:
            async with asyncio.timeout(self.settings.bff_run_max_step_seconds):
                if await self.commands.recover(claim):
                    await self.observer.observe(claim)
        except StaleRunLease:
            return True
        except asyncio.CancelledError:
            error = "BFFShutdown"
            raise
        except Exception as exc:
            error = type(exc).__name__
            with suppress(StaleRunLease):
                await asyncio.to_thread(
                    self.commands.results.blocked, claim, "reconciliation_required"
                )
        finally:
            renew.cancel()
            with suppress(asyncio.CancelledError):
                await renew
            with suppress(StaleRunLease):
                await asyncio.to_thread(
                    self.store.finish,
                    claim,
                    delay=self.settings.bff_run_reconcile_seconds,
                    error=error,
                )
        return True

    async def _lane(self, index):
        """Keep retrying durable due roots after database/network failures without busy polling."""
        while not self.stopping:
            try:
                if index == 0:
                    await asyncio.to_thread(self.store.reconcile_inbox)
                worked = await self.tick(lane=index)
                if worked:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # No sensitive remote payloads in logs; readiness reports a stalled loop.
                pass
            self.signal.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.signal.wait(), timeout=self.settings.bff_run_poll_seconds
                )

    async def healthy(self):
        """Report BFF loop freshness and overdue roots."""
        if (
            not self.tasks
            or any(task.done() for task in self.tasks)
            or self.last_tick is None
            or self.last_tick
            < now() - timedelta(seconds=self.settings.bff_run_ready_backlog_seconds)
        ):
            return False

        def check():
            """Read backlog without claiming work or mutating business state."""
            self.store.require_schema()
            with self.store.sessions() as session:
                oldest = session.scalar(
                    select(func.min(RootRunRow.due_at)).where(
                        RootRunRow.driver_version == self.store.driver_version,
                        RootRunRow.backend_instance_id == self.store.backend_instance_id,
                        RootRunRow.active.is_(True),
                    )
                )
                from financeclaw.shared.execution_ledger.root_repository import aware

                return oldest is None or aware(oldest) >= now() - timedelta(
                    seconds=self.settings.bff_run_ready_backlog_seconds
                )

        return await asyncio.to_thread(check)
