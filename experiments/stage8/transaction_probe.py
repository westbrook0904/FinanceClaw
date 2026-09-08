"""真实 PostgreSQL 的事务故障与多进程竞争验证；不以 SQLite 代替并发证据。"""

import multiprocessing
from datetime import timedelta

from sqlalchemy import func, select, update

from experiments.stage8.postgres_worker import claim_due, finish
from experiments.stage8.store import (
    Inbox,
    Lease,
    ProbeStore,
    ProgressEvent,
    Projection,
    StaleWriter,
    now,
)
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow


def counts(store: ProbeStore) -> list[int]:
    """事务前后比较所有事实表行数，不能留下半个已受理任务。"""
    with store.sessions() as session:
        return [
            session.scalar(select(func.count()).select_from(table))
            for table in (
                ConversationTurnRow,
                ConversationMessageRow,
                RunExecutionRow,
                RunOperationRow,
                Projection,
                Inbox,
                Lease,
                ProgressEvent,
            )
        ]


def competing_admission(database: str, conversation: str, results) -> None:
    """独立连接、独立进程竞争同一幂等键。"""
    store = ProbeStore(database)
    try:
        results.put(store.admit(conversation))
    finally:
        store.db.close()


def competing_lease(database: str, owner: str, results) -> None:
    """真实 SKIP LOCKED 竞争，不共享 Python 锁或数据库 Session。"""
    store = ProbeStore(database)
    try:
        results.put(claim_due(store, owner, ttl=30))
    finally:
        store.db.close()


def transaction_probes(store: ProbeStore, database: str) -> dict:
    """验证受理／完成各 flush 故障、唯一受理、旧租约和新唤醒保护。"""
    report = {}
    for point in ("journal", "snapshot", "operation", "projection", "inbox", "event"):
        conversation = store.create_conversation()
        before = counts(store)
        try:
            store.admit(conversation, fail_at=point)
        except RuntimeError as exc:
            assert "injected transaction failure" in str(exc)
        else:
            raise AssertionError("missing injected admission failure")
        assert counts(store) == before
    report["admission_rollback_points"] = 6
    conversation = store.create_conversation()
    root_id = store.admit(conversation)
    command = store.read(root_id)["payload"]["command"]
    store.execution.claim(command["operation_id"])
    store.execution.bind(command["operation_id"], "synthetic-exact-attempt")
    final = {
        "operation_id": command["operation_id"],
        "execution_id": "synthetic-exact-attempt",
        "result": {"synthetic": True},
    }
    before = store.read(root_id)
    baseline = counts(store)
    for point in ("assistant", "terminal", "projection", "event"):
        try:
            store.update(before, {"stage": "completed"}, final=final, fail_at=point)
        except RuntimeError as exc:
            assert "injected transaction failure" in str(exc)
        else:
            raise AssertionError("missing injected completion failure")
        assert counts(store) == baseline
        assert store.execution.operation(command["operation_id"])["status"] == "submitted"
        assert store.read(root_id)["revision"] == before["revision"]
    assert store.update(before, {"stage": "completed"}, final=final)
    # Retrying the old completion CAS cannot add a second assistant or event.
    assert not store.update(before, {"stage": "completed"}, final=final)
    assert len(store.journal.list_messages(conversation)) == 2
    report["completion_rollback_points"] = 4
    with store.sessions.begin() as session:
        session.execute(update(Lease).values(stopped=True))
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    conversation = store.create_conversation()
    processes = [
        context.Process(target=competing_admission, args=(database, conversation, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    roots = [results.get(timeout=1) for _ in processes]
    assert len(set(roots)) == 1
    assert len(store.journal.list_messages(conversation)) == 1
    report["concurrent_admission_processes"] = 4
    processes = [
        context.Process(target=competing_lease, args=(database, "owner-" + str(index), results))
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    claims = [results.get(timeout=1) for _ in processes]
    claims = [claim for claim in claims if claim]
    assert len(claims) == 1 and claims[0]["run_id"] == roots[0]
    old = claims[0]
    # A new wake leaves the active lease intact; old finish must preserve the wake.
    with store.locked(roots[0]) as (session, root):
        store.wake(session, root, "new-wake", "probe")
        session.flush()
        assert session.get(Lease, roots[0]).owner == old["owner"]
    assert finish(store, old, 30)
    second = claim_due(store, "second")
    assert second and second["epoch"] > old["epoch"]
    with store.sessions.begin() as session:
        session.get(Lease, roots[0]).until = now() - timedelta(seconds=1)
    third = claim_due(store, "third")
    assert third and third["epoch"] > second["epoch"]
    try:
        store.update(store.read(roots[0]), {"stage": "corrupted"}, second)
    except StaleWriter:
        pass
    else:
        raise AssertionError("stale worker updated the projection")
    assert not finish(store, second, 0)
    assert finish(store, third, None)
    frozen = store.execution.get(roots[0])["snapshot"]
    with store.sessions.begin() as session:
        session.get(Projection, roots[0]).driver_version = 2
    try:
        with store.locked(roots[0]):
            raise AssertionError("old driver accepted a newer stored protocol")
    except StaleWriter:
        pass
    with store.sessions.begin() as session:
        session.get(Projection, roots[0]).driver_version = 1
    with store.locked(roots[0]):
        assert store.execution.get(roots[0])["snapshot"] == frozen
    report["concurrent_lease_processes"] = 4
    report["new_wake_preserved_and_stale_writer_fenced"] = True
    report["driver_version_guard_and_original_snapshot_preserved"] = True
    return report
