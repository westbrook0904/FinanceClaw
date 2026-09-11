"""历史索引、删除恢复与保留边界的行为验收。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from financeclaw.agent_server.memory.history import HistoryService
from financeclaw.agent_server.memory.models import MemoryDraft
from financeclaw.integrations.history_indexer import HistoryIndexer
from financeclaw.shared.artifacts.tables import ArtifactMetadataRow
from financeclaw.shared.conversation.lifecycle import ConversationRetention
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.memory.deletion import (
    MEMORY_DELETE_DESTINATION,
    MemoryDeletionConsumer,
)
from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow
from tests.stage9.test_memory import CountingEmbeddings


class StoreClient:
    """仅测试中把 SDK 形状映射到真实 InMemoryStore，不代替生产 Store。"""

    def __init__(self, store):
        """共享原生 Store 及实际 embedding 计数器。"""
        self.store = store

    async def get_item(self, namespace, key, **kwargs):
        """模拟 SDK 的单条文档读取。"""
        item = self.store.get(namespace, key)
        return {"value": item.value, "key": item.key} if item else None

    async def put_item(self, namespace, key, value, **kwargs):
        """写入真实 Store 以触发原生索引。"""
        self.store.put(namespace, key, value, **kwargs)

    async def search_items(self, namespace, **kwargs):
        """模拟 SDK 搜索的 items 包装。"""
        return {
            "items": [
                {"key": item.key, "value": item.value}
                for item in self.store.search(namespace, **kwargs)
            ]
        }

    async def delete_item(self, namespace, key):
        """删除真实 Store 文档与索引。"""
        self.store.delete(namespace, key)


def complete(repository, context):
    """把当前合成 Turn 推进到正常完成并取得定向索引任务。"""
    from tests.turn_support import finish_turn

    finish_turn(repository, context.turn_id)
    repository.append_assistant_message(turn_id=context.turn_id, content="历史尾部的唯一决策" * 500)
    outbox = SqlAlchemyOutboxRepository(repository._sessions)
    return outbox.claim_pending(destination="history_index", limit=1)[0]


@pytest.mark.asyncio
async def test_history_index_retries_skips_unchanged_and_removes_obsolete_chunks(memory_stack):
    """原文尾部有索引；重复投递不再 embedding，重建不遗留旧块。"""
    context, _, repository, artifacts, _, _ = memory_stack
    event = complete(repository, context)
    embeddings = CountingEmbeddings()
    store = InMemoryStore(index={"dims": 3, "embed": embeddings, "fields": ["content"]})
    client = StoreClient(store)
    indexer = HistoryIndexer(repository, client, chunk_chars=200)
    await indexer.publish(event)
    indexed = embeddings.documents
    assert indexed > 10
    await indexer.publish(event)
    assert embeddings.documents == indexed
    await HistoryIndexer(repository, client, chunk_chars=1000, index_version="history/2").publish(
        event
    )
    namespace = HistoryService.namespace(context, context.conversation_id)
    values = store.search(namespace, limit=100)
    assert all(item.value["index_version"] == "history/2" for item in values)
    assert any(item.value["start"] > 2000 for item in values)
    service = HistoryService(repository, artifacts)
    assert service.search(context, store, "唯一决策")["matches"]
    with repository._sessions.begin() as session:
        for source in event.payload["sources"]:
            session.get(ConversationMessageRow, source["message_id"]).visible = False
    assert not service.search(context, store, "唯一决策")["matches"]


@pytest.mark.asyncio
async def test_delete_failure_has_persistent_retry_and_does_not_delete_new_profile(memory_stack):
    """失败任务无正文；后来同字段有新值时，旧消费者不能误删。"""
    context, identity, repository, _, service, _ = memory_stack
    outbox = SqlAlchemyOutboxRepository(repository._sessions)
    service.outbox = outbox

    class FailingStore(InMemoryStore):
        """可控制一次删除失败的真实 Store。"""

        fail_delete = False

        def delete(self, *args, **kwargs):
            """仅故障阶段拒绝删除，其余使用原生实现。"""
            if self.fail_delete:
                raise ConnectionError("Store unavailable")
            return super().delete(*args, **kwargs)

    store = FailingStore()
    draft = MemoryDraft(
        kind="preference", field="language", content="zh-CN", evidence_message_ids=(identity,)
    )
    record = service.save(context, store, draft=draft, mutation_id="first")
    store.fail_delete = True
    with pytest.raises(ConnectionError):
        service.forget(context, store, record.memory_id, mode="delete")
    task = outbox.claim_pending(destination=MEMORY_DELETE_DESTINATION, limit=1)[0]
    assert "content" not in task.payload and "zh-CN" not in str(task.payload)
    store.fail_delete = False
    consumer = MemoryDeletionConsumer(StoreClient(store), service.audit)
    await consumer.publish(task)
    assert store.get(record.namespace, "language") is None
    service.save(context, store, draft=draft, mutation_id="new")
    await consumer.publish(task)
    assert service.get(context, store, record.memory_id).mutation_id != record.mutation_id
    deleted = [item for item in service.audit.records() if item.action == "delete"]
    assert len(deleted) == 1


def test_outbox_destination_and_expired_claim_fencing(memory_stack):
    """不同消费者互不领取；旧租约不能覆盖已被重新领取的事件状态。"""
    context, _, repository, _, _, _ = memory_stack
    outbox = SqlAlchemyOutboxRepository(repository._sessions)
    event = OutboxEvent(
        event_id="targeted",
        event_type="probe",
        destination="history_index",
        aggregate_type="turn",
        aggregate_id=context.turn_id,
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
    )
    outbox.enqueue(event)
    assert outbox.claim_pending(limit=1, destination="audit") == ()
    old = outbox.claim_pending(limit=1, destination="history_index")[0]
    with repository._sessions.begin() as session:
        session.get(OutboxEventRow, event.event_id).locked_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
    new = outbox.claim_pending(limit=1, destination="history_index")[0]
    assert new.claim_epoch == old.claim_epoch + 1
    with pytest.raises(LookupError):
        outbox.mark_published(event.event_id, claim_epoch=old.claim_epoch)
    with pytest.raises(LookupError):
        outbox.mark_failed(event.event_id, "old", max_attempts=1, claim_epoch=old.claim_epoch)
    outbox.mark_published(event.event_id, claim_epoch=new.claim_epoch)


def test_expired_artifacts_remain_protected_until_turn_settles(memory_stack):
    """过期不打断活动运行；正常完成后才回收内容并更新回读目录。"""
    context, _, repository, artifacts, _, _ = memory_stack
    metadata = artifacts.persist(
        "原始工具数据",
        context=context,
        source_type="tool_result",
        source_id="call",
        idempotency_key="expiry",
    )
    with repository._sessions.begin() as session:
        session.get(ArtifactMetadataRow, metadata.artifact_id).expires_at = datetime.now(
            UTC
        ) - timedelta(days=1)
    retention = ConversationRetention(repository._sessions, artifacts.store)
    assert retention.cleanup_artifacts(apply=True)[0]["status"] == "protected"
    complete(repository, context)
    assert retention.cleanup_artifacts()[0]["status"] == "eligible"
    assert retention.cleanup_artifacts(apply=True)[0]["status"] == "deleted"
    with pytest.raises(FileNotFoundError):
        artifacts.store.get(metadata.storage_uri)
    page = HistoryService(repository, artifacts).read(context, context.turn_id)
    assert page["artifacts"][0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_checkpoint_cleanup_requires_archived_business_and_idle_native_state(memory_stack):
    """业务已完成仍不够：活动会话及原生 interrupt 必须阻止回收。"""
    context, _, repository, artifacts, _, _ = memory_stack
    complete(repository, context)
    called = []
    from unittest.mock import AsyncMock

    from financeclaw.api.application.maintenance import CheckpointMaintenance

    native = SimpleNamespace(
        threads=SimpleNamespace(
            get=AsyncMock(return_value={"status": "idle"}),
            get_state=AsyncMock(return_value={"next": [], "tasks": []}),
            prune=AsyncMock(
                side_effect=lambda ids, **kwargs: (
                    called.append((ids, kwargs)) or {"pruned_count": len(ids)}
                )
            ),
        )
    )
    retention = ConversationRetention(repository._sessions, artifacts.store)
    arguments = dict(
        conversation_id=context.conversation_id,
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
        apply=True,
    )
    with pytest.raises(ValueError, match="archived"):
        await CheckpointMaintenance(retention, native).prune(**arguments)
    with repository._sessions.begin() as session:
        session.get(ConversationRow, context.conversation_id).status = "archived"
    native.threads.get_state = AsyncMock(return_value={"tasks": [{"interrupts": ["pending"]}]})
    with pytest.raises(ValueError, match="pending"):
        await CheckpointMaintenance(retention, native).prune(**arguments)
    assert not called
    native.threads.get_state = AsyncMock(return_value={"next": [], "tasks": []})
    assert (await CheckpointMaintenance(retention, native).prune(**arguments))["applied"]
    assert called[0][1]["strategy"] == "keep_latest"


def test_history_rebuild_is_owner_scoped_and_does_not_steal_a_live_consumer(memory_stack):
    """重建只重新入队指定会话的完成历史，不能覆盖尚在执行的消费者。"""
    from financeclaw.shared.conversation.indexing import requeue_history

    context, _, repository, _, _, _ = memory_stack
    event = complete(repository, context)
    outbox = SqlAlchemyOutboxRepository(repository._sessions)
    args = dict(
        conversation_id=context.conversation_id,
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
        apply=True,
    )
    in_progress = requeue_history(repository._sessions, **args)
    assert in_progress["turns"][0]["status"] == "consumer_in_progress"
    outbox.mark_published(event.event_id, claim_epoch=event.claim_epoch)
    with pytest.raises(LookupError):
        requeue_history(repository._sessions, **{**args, "subject_id": "unrelated-owner"})
    result = requeue_history(repository._sessions, **args)
    assert result["turns"] == [{"turn_id": context.turn_id, "status": "queued"}]
    claimed = outbox.claim_pending(limit=1, destination="history_index")[0]
    assert claimed.payload == event.payload and claimed.claim_epoch > event.claim_epoch
