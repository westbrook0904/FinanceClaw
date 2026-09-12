"""Independent durable history, memory-index and privacy-cleanup consumers."""

import asyncio
import json
from hashlib import sha256

from financeclaw.integrations.history_indexer import HistoryIndexer
from financeclaw.integrations.memory_indexer import MemoryIndexer
from financeclaw.shared.conversation.indexing import HISTORY_INDEX_DESTINATION
from financeclaw.shared.outbox.publisher import OutboxPublisher


def index_version(settings):
    """Fingerprint embedding configuration so stale history indexes can be rebuilt."""
    signature = {
        "schema": settings.history_index_version,
        "model": settings.embedding_model,
        "base_url": settings.embedding_base_url,
        "dimensions": settings.embedding_dimensions,
        "offline": settings.offline_model,
    }
    return sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


async def publish_loop(publisher, stop, poll_seconds):
    """Keep one destination's remote latency and failures out of the other consumers."""
    while not stop.is_set():
        await publisher.run_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


async def consume(resources, store, stop):
    """Fail the integrations role if any required consumer loop unexpectedly stops."""
    settings = resources.settings
    indexer = HistoryIndexer(
        resources.conversation_repository, store, index_version=index_version(settings)
    )
    memory = MemoryIndexer(
        resources.database.session_factory,
        store,
        resources.outbox_repository,
        index_version=settings.memory_index_version,
    )
    destinations = (
        (HISTORY_INDEX_DESTINATION, indexer, settings.history_index_batch_size),
        ("memory_index", memory, 4),
        ("memory_index_delete", memory, 4),
    )
    async with asyncio.TaskGroup() as group:
        for destination, sink, batch_size in destinations:
            publisher = OutboxPublisher(
                resources.outbox_repository,
                sink,
                destination=destination,
                batch_size=batch_size,
                max_attempts=settings.outbox_max_attempts,
            )
            group.create_task(
                publish_loop(publisher, stop, settings.history_index_poll_seconds), name=destination
            )
