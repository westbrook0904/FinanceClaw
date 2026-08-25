"""StateStore SPI、内存实现与 SQLite JSON Snapshot 测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness_contracts import (
    ExecutionPlan,
    InvocationContext,
    NodeExecutionState,
    PlanExecutionRecord,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    Request,
    RequestInput,
)
from harness_state import (
    InMemoryStateStore,
    SQLiteStateStore,
    StateRecordExistsError,
    StateRecordNotFoundError,
)


def make_record(plan_id: str = "state-plan") -> PlanExecutionRecord:
    request = Request(
        request_id="state-request",
        input=RequestInput(type="json", content={"seed": 7}),
    )
    plan = ExecutionPlan(
        plan_id=plan_id,
        nodes=(PlanNode(node_id="work", capability="state.work/v1"),),
    )
    state = PlanExecutionState(
        plan_id=plan_id,
        plan_revision=plan.revision,
        nodes={"work": NodeExecutionState(node_id="work")},
    )
    return PlanExecutionRecord(
        plan_id=plan_id,
        plan=plan,
        context=InvocationContext(request=request),
        state=state,
    )


class InMemoryStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_load_save_delete_use_detached_snapshots(self) -> None:
        store = InMemoryStateStore()
        record = make_record()

        await store.create(record)
        record.state.status = PlanExecutionStatus.RUNNING
        record.state.state_version += 1

        created = await store.load(record.plan_id)
        self.assertEqual(created.state.status, PlanExecutionStatus.CREATED)
        self.assertEqual(created.state_version, 1)

        await store.save(record)
        saved = await store.load(record.plan_id)
        self.assertEqual(saved.state.status, PlanExecutionStatus.RUNNING)
        self.assertEqual(saved.state_version, 2)

        await store.delete(record.plan_id)
        self.assertIsNone(await store.load(record.plan_id))
        await store.delete(record.plan_id)

    async def test_create_duplicate_and_save_missing_are_explicit(self) -> None:
        store = InMemoryStateStore()
        record = make_record()
        await store.create(record)

        with self.assertRaises(StateRecordExistsError):
            await store.create(record)
        await store.delete(record.plan_id)
        with self.assertRaises(StateRecordNotFoundError):
            await store.save(record)


class SQLiteStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_memory_database_keeps_snapshots_between_operations(self) -> None:
        store = SQLiteStateStore(":memory:")
        record = make_record("sqlite-memory")

        await store.create(record)
        loaded = await store.load(record.plan_id)

        self.assertEqual(loaded, record)

    async def test_json_snapshot_round_trip_survives_store_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            first_store = SQLiteStateStore(database)
            record = make_record("sqlite-plan")

            await first_store.create(record)
            record.state.status = PlanExecutionStatus.RUNNING
            record.state.state_version += 1
            await first_store.save(record)

            second_store = SQLiteStateStore(database)
            loaded = await second_store.load(record.plan_id)

            self.assertEqual(loaded, record)
            self.assertEqual(loaded.state_version, 2)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    """
                    SELECT state_version, payload_json, created_at, updated_at
                    FROM plan_execution_records WHERE plan_id = ?
                    """,
                    (record.plan_id,),
                ).fetchone()
            self.assertEqual(row[0], 2)
            self.assertEqual(json.loads(row[1])["plan_id"], record.plan_id)
            self.assertTrue(row[2])
            self.assertTrue(row[3])

    async def test_sqlite_create_save_and_delete_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            record = make_record("sqlite-contract")
            await store.create(record)

            with self.assertRaises(StateRecordExistsError):
                await store.create(record)
            await store.delete(record.plan_id)
            self.assertIsNone(await store.load(record.plan_id))
            with self.assertRaises(StateRecordNotFoundError):
                await store.save(record)


if __name__ == "__main__":
    unittest.main()
