"""Exact-version Store indexing, late writes and honest physical-purge status."""

import asyncio

import pytest

from financeclaw.integrations.memory_indexer import MemoryIndexer, PendingIndexWrites
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.memory.models import MemoryActor, MemoryMutation
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.namespace import memory_index_namespace
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository


class ControlledStore:
    """Model remote completion order while retaining exact namespace and key writes."""

    def __init__(self):
        """Start with an empty projection and no injected remote fault."""
        self.values = {}
        self.before_write = None
        self.timeout_after_write = False

    async def put_item(self, namespace, key, value, *, index):
        """Allow a SQL deletion to commit while an older remote write is in flight."""
        if self.before_write:
            await self.before_write()
        self.values[tuple(namespace), key] = value
        if self.timeout_after_write:
            raise TimeoutError("response unknown")

    async def delete_item(self, namespace, key):
        """Delete one exact revision; newer or foreign keys remain untouched."""
        self.values.pop((tuple(namespace), key), None)


@pytest.fixture
def index_stack(tmp_path):
    """Create a real SQL task memory and its transactionally admitted index event."""
    database = ApplicationDatabase(f"sqlite:///{tmp_path / 'index.db'}")
    database.initialize_schema()
    actor = MemoryActor(
        tenant_id="tenant",
        subject_id="subject",
        scopes=frozenset({"memory:read", "memory:write", "memory:delete"}),
    )
    mutations = MemoryMutationService(database.session_factory)
    receipt = mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="task", kind="task", content="2026 年曾分析债券久期。", explicit_intent=True
        ),
    )
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    event = outbox.claim_pending(destination="memory_index", limit=1)[0]
    store = ControlledStore()
    indexer = MemoryIndexer(database.session_factory, store, outbox)
    yield database, actor, mutations, receipt, outbox, event, store, indexer
    database.close()


@pytest.mark.asyncio
async def test_projection_uses_sql_body_and_exact_version(index_stack):
    """S39: payload content is not authoritative and each revision has an immutable key."""
    _, actor, _, receipt, outbox, event, store, indexer = index_stack
    await indexer.publish(event)
    key = memory_index_namespace(actor, "memory-v1"), f"{receipt.memory_id}:1"
    assert store.values[key]["content"] == "2026 年曾分析债券久期。"
    assert store.values[key]["revision"] == 1
    assert outbox.get(event.event_id).processing_metadata["index_writes"] == {"1": "acknowledged"}


@pytest.mark.asyncio
async def test_forget_during_old_write_removes_exact_key(index_stack):
    """S32: a late acknowledged put cannot survive the SQL post-write validity check."""
    _, actor, mutations, receipt, _, event, store, indexer = index_stack

    async def forget():
        """Commit privacy invalidation after remote I/O has already been reserved."""
        await asyncio.to_thread(
            mutations.apply,
            actor,
            MemoryMutation(
                mutation_id="forget",
                operation="forget",
                memory_id=receipt.memory_id,
                expected_revision=1,
                explicit_intent=True,
            ),
        )

    store.before_write = forget
    store.values[(memory_index_namespace(actor, "memory-v1"), "different:5")] = {"content": "keep"}
    await indexer.publish(event)
    assert (
        memory_index_namespace(actor, "memory-v1"),
        f"{receipt.memory_id}:1",
    ) not in store.values
    assert store.values[(memory_index_namespace(actor, "memory-v1"), "different:5")] == {
        "content": "keep"
    }


@pytest.mark.asyncio
async def test_unknown_write_prevents_false_verified_purge(index_stack):
    """Timeout remains pending even when one delete succeeds; it may have remote late effects."""
    _, actor, mutations, receipt, outbox, event, store, indexer = index_stack
    store.timeout_after_write = True
    with pytest.raises(TimeoutError):
        await indexer.publish(event)
    mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="forget",
            operation="forget",
            memory_id=receipt.memory_id,
            expected_revision=1,
            explicit_intent=True,
        ),
    )
    deletion = outbox.claim_pending(destination="memory_index_delete", limit=1)[0]
    with pytest.raises(PendingIndexWrites):
        await indexer.publish(deletion)
    assert outbox.get(deletion.event_id).processing_metadata["purge_status"] == "pending"
    assert outbox.get(event.event_id).processing_metadata["index_writes"] == {"1": "unknown"}


@pytest.mark.asyncio
async def test_foreign_owner_cannot_project_source_body(index_stack):
    """S51: attacker namespace construction cannot read the owner's record body."""
    _, actor, _, receipt, _, event, store, indexer = index_stack
    foreign = event.model_copy(update={"tenant_id": "other"})
    await indexer.publish(foreign)
    assert not store.values


@pytest.mark.asyncio
async def test_unrelated_privacy_epoch_change_does_not_lose_valid_index_task(index_stack):
    """An older queued event rebuilds from the current valid SQL source after unrelated forget."""
    database, actor, _, receipt, _, event, store, indexer = index_stack
    from financeclaw.shared.memory.tables import MemoryOwnerRow

    with database.session_factory.begin() as session:
        session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id)).privacy_epoch += 1
    await indexer.publish(event)
    key = memory_index_namespace(actor, "memory-v1"), f"{receipt.memory_id}:1"
    assert store.values[key]["privacy_epoch"] == 1


@pytest.mark.asyncio
async def test_reindex_creates_fresh_event_after_published_delivery_and_ignores_forgotten(
    index_stack,
):
    """A lost Store can be rebuilt from active SQL facts without changing memory revisions."""
    database, actor, mutations, receipt, outbox, event, store, indexer = index_stack
    from financeclaw.memory_worker.operations import MemoryOperations
    from financeclaw.shared.memory.tables import MemoryOwnerRow

    await indexer.publish(event)
    outbox.mark_published(event.event_id, claim_epoch=event.claim_epoch)
    store.values.clear()
    operations = MemoryOperations(
        database.session_factory,
        extraction_fingerprint="unused",
        consolidation_fingerprint="unused",
    )
    result = operations.reindex(
        tenant_id=actor.tenant_id,
        subject_id=actor.subject_id,
        operator="test-operator",
        reason="Store rebuild",
        request_id="rebuild-1",
    )
    assert result["scheduled"] == 1
    rebuild = outbox.claim_pending(destination="memory_index", limit=1)[0]
    assert rebuild.event_id != event.event_id
    await indexer.publish(rebuild)
    assert store.values
    with database.session_factory() as session:
        assert session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id)).memory_revision == 1
    mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="forget-after-rebuild",
            operation="forget",
            memory_id=receipt.memory_id,
            expected_revision=1,
            explicit_intent=True,
        ),
    )
    result = operations.reindex(
        tenant_id=actor.tenant_id,
        subject_id=actor.subject_id,
        operator="test-operator",
        reason="Store rebuild",
        request_id="rebuild-2",
    )
    assert result["scheduled"] == 0
