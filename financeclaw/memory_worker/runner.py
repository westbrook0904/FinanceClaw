"""Durable Outbox processing with independent slots and lease fencing."""

import asyncio
import logging
from typing import Protocol

from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.repository import ModelBudgetExhausted, SqlAlchemyOutboxRepository

logger = logging.getLogger(__name__)


class JobHandler(Protocol):
    """A handler atomically commits its result and the Outbox completion."""

    async def process(self, event: OutboxEvent) -> None:
        """Process one claimed job without holding a transaction over model I/O."""
        ...

    def fail(self, event: OutboxEvent, reason: str, *, terminal: bool) -> None:
        """Retry or quarantine exact inputs and release their owner wakeup atomically."""
        ...


class MemoryJobRunner:
    """Each slot claims one job; renewals span reading, generation, and commit."""

    def __init__(
        self,
        repository: SqlAlchemyOutboxRepository,
        handler: JobHandler,
        *,
        destination: str,
        concurrency: int = 1,
        lease_seconds: int = 120,
        renew_seconds: float = 20,
        poll_seconds: float = 1,
    ) -> None:
        """Validate intervals so a healthy request never outlives its lease."""
        if concurrency < 1 or not 0 < renew_seconds < lease_seconds / 2 or poll_seconds <= 0:
            raise ValueError("invalid memory worker concurrency or lease intervals")
        self.repository = repository
        self.handler = handler
        self.destination = destination
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.renew_seconds = renew_seconds
        self.poll_seconds = poll_seconds

    async def run_once(self) -> int:
        """Run at most one claimed task per slot; database calls stay off the event loop."""
        counts = await asyncio.gather(*(self._run_slot() for _ in range(self.concurrency)))
        return sum(counts)

    async def _run_slot(self) -> int:
        """Race job work against lease failure, never accepting work from an expired owner."""
        events = await asyncio.to_thread(
            self.repository.claim_pending,
            limit=1,
            destination=self.destination,
            lease_seconds=self.lease_seconds,
        )
        if not events:
            return 0
        event = events[0]
        work = asyncio.create_task(self.handler.process(event))
        renewal = asyncio.create_task(self._renew(event))
        try:
            done, _ = await asyncio.wait({work, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await renewal
        except asyncio.CancelledError:
            raise
        except LookupError:
            # Commit acknowledgements may be lost. Ownership checks, not a second
            # unguarded write, decide whether a later consumer may resume the job.
            logger.warning("memory_job_claim_lost", extra={"event_id": event.event_id})
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    self.handler.fail,
                    event,
                    type(exc).__name__,
                    terminal=isinstance(exc, (ModelBudgetExhausted, UnsupportedPipeline)),
                )
            except LookupError:
                logger.warning("memory_job_failure_claim_lost", extra={"event_id": event.event_id})
            logger.warning(
                "memory_job_failed",
                extra={
                    "event_id": event.event_id,
                    "reason": type(exc).__name__,
                },
            )
        finally:
            for task in (work, renewal):
                task.cancel()
            await asyncio.gather(work, renewal, return_exceptions=True)
        return 1

    async def _renew(self, event: OutboxEvent) -> None:
        """Abort processing on renewal failure without authorizing any stale commit."""
        while True:
            await asyncio.sleep(self.renew_seconds)
            await asyncio.to_thread(
                self.repository.renew_claim,
                event.event_id,
                claim_epoch=event.claim_epoch,
                lease_seconds=self.lease_seconds,
            )

    async def consume(self, stop: asyncio.Event) -> None:
        """Keep independent slot loops alive until shutdown, without long polling sleeps."""

        async def slot_loop() -> None:
            """Let each slot make progress independently of a slower sibling."""
            while not stop.is_set():
                count = await self._run_slot()
                if not count:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                    except TimeoutError:
                        pass

        async with asyncio.TaskGroup() as group:
            for _ in range(self.concurrency):
                group.create_task(slot_loop())


class UnsupportedPipeline(RuntimeError):
    """Frozen source jobs must not silently run a different prompt or model version."""
