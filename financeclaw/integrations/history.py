"""History indexing and memory deletion consume durable outbox intents in integrations."""

import asyncio
import json
from hashlib import sha256

from financeclaw.integrations.history_indexer import HistoryIndexer
from financeclaw.shared.conversation.indexing import HISTORY_INDEX_DESTINATION
from financeclaw.shared.memory.deletion import MEMORY_DELETE_DESTINATION, MemoryDeletionConsumer
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


async def consume(resources, store, stop):
    """Publish history and deletion outbox intents independently of API request handling."""
    settings = resources.settings
    indexer = HistoryIndexer(
        resources.conversation_repository, store, index_version=index_version(settings)
    )
    publishers = (
        OutboxPublisher(
            resources.outbox_repository,
            indexer,
            destination=HISTORY_INDEX_DESTINATION,
            batch_size=settings.history_index_batch_size,
        ),
        OutboxPublisher(
            resources.outbox_repository,
            MemoryDeletionConsumer(store, resources.audit),
            destination=MEMORY_DELETE_DESTINATION,
            batch_size=4,
        ),
    )
    while not stop.is_set():
        for publisher in publishers:
            await publisher.run_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.history_index_poll_seconds)
        except TimeoutError:
            pass
