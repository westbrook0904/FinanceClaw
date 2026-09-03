"""Non-blocking outbox drain loop with bounded failure handling."""

from typing import Protocol

from .models import OutboxEvent
from .repository import OutboxRepository


class OutboxSink(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...


class OutboxPublisher:
    def __init__(
        self,
        repository: OutboxRepository,
        sink: OutboxSink,
        *,
        batch_size: int = 100,
        max_attempts: int = 8,
    ) -> None:
        self._repository = repository
        self._sink = sink
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        """Publish one claimed batch; callers schedule retries outside request handling."""

        events = self._repository.claim_pending(limit=self._batch_size)
        for event in events:
            try:
                await self._sink.publish(event)
            except Exception as exc:
                self._repository.mark_failed(
                    event.event_id,
                    f"{type(exc).__name__}: {exc}",
                    max_attempts=self._max_attempts,
                )
            else:
                self._repository.mark_published(event.event_id)
        return len(events)
