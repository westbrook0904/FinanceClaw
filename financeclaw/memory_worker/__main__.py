"""Run the independent bounded memory extraction and consolidation process."""

import asyncio
import json
import signal
import time

from sqlalchemy import func, select

from financeclaw.memory_worker.bootstrap import build_memory_worker
from financeclaw.memory_worker.health import HEALTH_PATH
from financeclaw.shared.infrastructure.runtime import configure_observability
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.outbox.tables import OutboxEventRow


def queue_status(database):
    """Expose destination/status counts without reading payloads or user content."""
    with database.session_factory() as session:
        return [
            {"destination": destination, "status": status, "count": count}
            for destination, status, count in session.execute(
                select(OutboxEventRow.destination, OutboxEventRow.status, func.count())
                .where(OutboxEventRow.destination.in_(["memory_extract", "memory_consolidate"]))
                .group_by(OutboxEventRow.destination, OutboxEventRow.status)
            )
        ]


async def heartbeat(resources, workers, stop):
    """Persist actual consumer health and queue status for a container health probe."""
    while not stop.is_set():
        if any(task.done() for task in workers):
            raise RuntimeError("memory consumer stopped unexpectedly")
        queues = await asyncio.to_thread(queue_status, resources.database)
        temporary = HEALTH_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"time": time.time(), "healthy": True, "queues": queues}), encoding="utf-8"
        )
        temporary.replace(HEALTH_PATH)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


async def main():
    """Drain bounded in-flight work on signal; unexpected consumer failure stops the role."""
    settings = FinanceClawSettings(_env_file=None)
    telemetry = configure_observability(settings)
    resources = build_memory_worker(settings)
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    workers = [
        asyncio.create_task(runner.consume(stop))
        for runner in (resources.extraction, resources.consolidation)
    ]
    health = asyncio.create_task(heartbeat(resources, workers, stop))
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            [*workers, health, waiter], return_when=asyncio.FIRST_COMPLETED
        )
        if waiter not in done:
            for task in done:
                await task
    finally:
        stop.set()
        try:
            async with asyncio.timeout(settings.shutdown_timeout_seconds):
                await asyncio.gather(*workers, return_exceptions=True)
        except TimeoutError:
            for task in workers:
                task.cancel()
        for task in (health, waiter):
            task.cancel()
        await asyncio.gather(*workers, health, waiter, return_exceptions=True)
        HEALTH_PATH.unlink(missing_ok=True)
        resources.database.close()
        telemetry.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
