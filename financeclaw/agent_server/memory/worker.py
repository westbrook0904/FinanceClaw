"""历史索引后台 role：复用 outbox 和 Agent Server Store API。"""

import argparse
import asyncio
import json
from hashlib import sha256

from langgraph_sdk import get_client

from financeclaw.agent_server.memory.deletion import (
    MEMORY_DELETE_DESTINATION,
    MemoryDeletionConsumer,
)
from financeclaw.agent_server.memory.indexing import HistoryIndexer
from financeclaw.shared.conversation.indexing import HISTORY_INDEX_DESTINATION
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.outbox.publisher import OutboxPublisher


def index_version(settings: FinanceClawSettings) -> str:
    """索引格式或实际向量服务变化时，已加工的历史块必须重新编码。"""
    signature = {
        "schema": settings.history_index_version,
        "model": settings.embedding_model,
        "base_url": settings.embedding_base_url,
        "dimensions": settings.embedding_dimensions,
        "offline": settings.offline_model,
    }
    return sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


async def run(*, once: bool = False) -> None:
    """独立后台进程，退出时关闭数据库资源。"""
    settings = FinanceClawSettings()
    resources = build_resources(settings, enable_persistence=True)
    headers = (
        {"Authorization": f"Bearer {settings.agent_server_service_token.get_secret_value()}"}
        if settings.agent_server_service_token
        else None
    )
    client = get_client(url=settings.agent_server_url, headers=headers)
    indexer = HistoryIndexer(
        resources.conversation_repository,
        client.store,
        index_version=index_version(settings),
    )
    publisher = OutboxPublisher(
        resources.outbox_repository,
        indexer,
        destination=HISTORY_INDEX_DESTINATION,
        batch_size=settings.history_index_batch_size,
    )
    deletions = OutboxPublisher(
        resources.outbox_repository,
        MemoryDeletionConsumer(client.store, resources.audit),
        destination=MEMORY_DELETE_DESTINATION,
        batch_size=4,
    )
    try:
        while True:
            await deletions.run_once()
            await publisher.run_once()
            if once:
                return
            await asyncio.sleep(settings.history_index_poll_seconds)
    finally:
        resources.database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(run(once=parser.parse_args().once))
