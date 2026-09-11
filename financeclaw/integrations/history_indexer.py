"""完成 Turn 的后台派生索引；通过 Agent Server SDK 使用原生 Store。"""

import asyncio
from hashlib import sha256

from langgraph_sdk.errors import NotFoundError

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.conversation.indexing import HISTORY_INDEX_DESTINATION
from financeclaw.shared.memory.namespace import history_namespace
from financeclaw.shared.memory.observability import store_operation


class HistoryIndexer:
    """确定性切块、来源复验及重复投递去重，不调用生成模型。"""

    def __init__(self, conversations, store_client, *, index_version="history/1", chunk_chars=1200):
        """固定切块长度与索引版本，支持确定性的失败重入。"""
        if not 128 <= chunk_chars <= 8000:
            raise ValueError("invalid history chunk size")
        self.conversations = conversations
        self.store = store_client
        self.index_version = index_version
        self.chunk_chars = chunk_chars

    async def publish(self, event):
        """消费一条定向事件；来源被删除时清理派生索引。"""
        if (
            event.destination != HISTORY_INDEX_DESTINATION
            or event.event_type != "history.index.requested"
        ):
            raise ValueError("history indexer received a different destination")
        payload = event.payload
        context = ExecutionContext(
            tenant_id=event.tenant_id,
            subject_id=event.subject_id,
            conversation_id=payload["conversation_id"],
            turn_id=payload["turn_id"],
            scopes={"memory:read"},
        )
        namespace = history_namespace(context, context.conversation_id)
        try:
            turn = await asyncio.to_thread(
                self.conversations.get_turn_owned,
                context.turn_id,
                context.tenant_id,
                context.subject_id,
            )
            if turn.status.value != "completed":
                return
            await asyncio.to_thread(
                self.conversations.get_owned,
                context.conversation_id,
                context.tenant_id,
                context.subject_id,
            )
        except LookupError:
            await self._remove_turn(namespace, context.turn_id)
            return
        expected = set()
        for reference in payload["sources"]:
            try:
                source = await asyncio.to_thread(
                    self.conversations.get_message_owned,
                    reference["message_id"],
                    context.tenant_id,
                    context.subject_id,
                )
            except LookupError:
                await self._remove_turn(namespace, context.turn_id)
                return
            if (
                source.turn_id != context.turn_id
                or source.conversation_id != context.conversation_id
                or source.content_hash != reference["content_hash"]
                or not source.visible
            ):
                raise ValueError("history source does not match the completed Turn")
            for start in range(0, len(source.content), self.chunk_chars):
                end = min(len(source.content), start + self.chunk_chars)
                key = f"{source.message_id}:{start}"
                expected.add(key)
                fingerprint = sha256(
                    f"{self.index_version}:{source.content_hash}:{start}:{end}".encode()
                ).hexdigest()
                try:
                    existing = await self.store.get_item(namespace, key, refresh_ttl=False)
                except NotFoundError:
                    existing = None
                if existing and existing["value"].get("index_fingerprint") == fingerprint:
                    continue
                with store_operation("history_index", index_items=1):
                    await self.store.put_item(
                        namespace,
                        key,
                        {
                            "content": f"{source.role.value}: {source.content[start:end]}",
                            "conversation_id": context.conversation_id,
                            "turn_id": context.turn_id,
                            "message_id": source.message_id,
                            "content_hash": source.content_hash,
                            "start": start,
                            "end": end,
                            "index_fingerprint": fingerprint,
                            "index_version": self.index_version,
                            "historical": True,
                        },
                        index=["content"],
                    )
            # A deletion during I/O must not leave a newly visible historical document.
            try:
                current = await asyncio.to_thread(
                    self.conversations.get_message_owned,
                    source.message_id,
                    context.tenant_id,
                    context.subject_id,
                )
                if not current.visible or current.content_hash != source.content_hash:
                    await self._remove_turn(namespace, context.turn_id)
                    return
            except LookupError:
                await self._remove_turn(namespace, context.turn_id)
                return
        await self._remove_obsolete(namespace, context.turn_id, expected)

    async def _remove_obsolete(self, namespace, turn_id, expected):
        """重建切块策略后移除多余旧块；不会触及其他 Turn 或画像。"""
        offset = 0
        while True:
            result = await self.store.search_items(
                namespace, filter={"turn_id": turn_id}, limit=100, offset=offset
            )
            items = result.get("items", [])
            obsolete = [item for item in items if item["key"] not in expected]
            for item in obsolete:
                await self.store.delete_item(namespace, item["key"])
            if len(items) < 100:
                return
            offset += len(items) - len(obsolete)

    async def _remove_turn(self, namespace, turn_id):
        """分批删除已不存在的 Turn 派生索引。"""
        while True:
            result = await self.store.search_items(
                namespace, filter={"turn_id": turn_id}, limit=100
            )
            items = result.get("items", [])
            if not items:
                return
            for item in items:
                await self.store.delete_item(namespace, item["key"])
