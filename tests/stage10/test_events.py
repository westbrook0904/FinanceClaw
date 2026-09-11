"""Snapshot subscriptions and shutdown never own native execution."""

import asyncio
import threading

import pytest
from sqlalchemy import event

from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.stage10.runtime import OWNER, admit, final_state, tick
from tests.stage10.runtime import runtime as runtime


@pytest.mark.asyncio
async def test_streams_share_batched_reads_and_reconnect_to_latest(runtime):
    """Multiple clients receive the latest revision without replay logs or per-client polling."""
    accepted = await admit(runtime)
    service = runtime.turns
    service.events.refresh_seconds = 0.03
    await service.events.start()
    first = service.stream(accepted.turn_id, **OWNER)
    second = service.stream(accepted.turn_id, **OWNER, last_event_id="invalid")
    assert (await anext(first)).id == (await anext(second)).id
    assert len(service.events._subscribers) == 1
    assert len(service.events._subscribers[accepted.turn_id]) == 2
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)

    async def terminal(stream):
        """Consume revision snapshots until the final durable answer is projected."""
        async for value in stream:
            if value.data.get("status") == "completed":
                return value

    try:
        values = await asyncio.wait_for(asyncio.gather(terminal(first), terminal(second)), 2)
        assert values[0].id == values[1].id
        reconnect = service.stream(accepted.turn_id, **OWNER, last_event_id="another-turn:999")
        assert (await anext(reconnect)).id == values[0].id
        await reconnect.aclose()
        assert len(runtime.client.calls) == 1
    finally:
        await first.aclose()
        await second.aclose()
        await service.events.stop()
    assert not service.events._subscribers


@pytest.mark.asyncio
async def test_snapshot_batch_query_count_does_not_grow_with_turns(runtime):
    """One Turn query and one interaction query suffice for any batch of unfinished Turns."""
    accepted = [await admit(runtime) for _ in range(16)]
    queries = []
    engine = runtime.resources.database.engine

    def collect(connection, cursor, statement, parameters, context, many):
        """Count actual SQL statements rather than inferring cost from implementation shape."""
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", collect)
    try:
        result = runtime.turns.events._batch([item.turn_id for item in accepted])
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert len(result) == 16 and len(queries) == 2
    assert all("release_snapshot" not in statement for statement in queries)


@pytest.mark.asyncio
async def test_shutdown_drains_claim_transaction_and_releases_lease(runtime, monkeypatch):
    """Drain a committed claim before propagating cancellation to resource shutdown."""
    accepted = await admit(runtime)
    original = runtime.turns.store.claim_due
    committed, release = threading.Event(), threading.Event()

    def claim(*args, **kwargs):
        """Pause after the SQL commit but before the scanner receives ownership."""
        result = original(*args, **kwargs)
        committed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(runtime.turns.store, "claim_due", claim)
    task = asyncio.create_task(runtime.turns.lifecycle._scan_once())
    try:
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    with runtime.turns.sessions() as session:
        row = session.get(ConversationTurnRow, accepted.turn_id)
        assert row.lease_owner is None and row.lease_until is None
    assert not runtime.client.calls


@pytest.mark.asyncio
async def test_database_outage_does_not_kill_scanner(runtime, monkeypatch):
    """The process retries database availability instead of silently losing background ownership."""
    called = asyncio.Event()

    async def fail_once():
        """Keep the second iteration parked after proving the first failure was supervised."""
        if not called.is_set():
            called.set()
            raise ConnectionError("database unavailable")
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.turns.lifecycle, "_scan_once", fail_once)
    task = asyncio.create_task(runtime.turns.lifecycle._scan())
    try:
        await asyncio.wait_for(called.wait(), 1)
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_readiness_distinguishes_abandoned_work_from_a_valid_lease(runtime):
    """Readiness fails overdue responsibility but accepts a live observer and terminal history."""
    from datetime import timedelta

    from financeclaw.shared.turns.types import now

    accepted = await admit(runtime)
    repository = runtime.turns.store
    with repository.sessions.begin() as session:
        row = session.get(ConversationTurnRow, accepted.turn_id)
        row.next_action_at = now() - timedelta(minutes=10)
    assert not repository.responsibility_healthy()
    lease = repository.claim_due("active")
    assert repository.responsibility_healthy()
    repository.release(lease, delay=0)
    assert repository.responsibility_healthy()
