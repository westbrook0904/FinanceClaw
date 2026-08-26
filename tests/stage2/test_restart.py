"""Stage 2 SQLite restart / crash-recovery acceptance tests."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness_contracts import (
    CapabilityExecutionProfile,
    Continuation,
    ExecutionPlan,
    IdempotencyType,
    NodeExecutionStatus,
    NodeOutputBinding,
    PlanEdge,
    PlanExecutionStatus,
    PlanNode,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    SideEffectType,
)
from harness_state import SQLiteStateStore

from tests.stage2.support import BlockingTool, EchoTool, ScriptedTool, make_engine, make_request


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_read_checkpoint_replays_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live_database = Path(directory) / "live.db"
            restart_database = Path(directory) / "restart.db"
            first_tool = BlockingTool(
                "restart.read/v1",
                profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
            )
            first = make_engine(first_tool, state_store=SQLiteStateStore(live_database))
            plan = ExecutionPlan(
                plan_id="restart-running-read",
                nodes=(PlanNode(node_id="work", capability="restart.read/v1"),),
            )

            execution = asyncio.create_task(first.engine.execute(make_request(), plan))
            await asyncio.wait_for(first_tool.started.wait(), timeout=1)
            running = await SQLiteStateStore(live_database).load(plan.plan_id)
            self.assertEqual(running.state.nodes["work"].status, NodeExecutionStatus.RUNNING)

            restart_store = SQLiteStateStore(restart_database)
            await restart_store.create(running)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution

            resumed_tool = ScriptedTool(
                "restart.read/v1",
                (ResultEnvelope.success(ResultOutput(type="json", data={"value": 9})),),
                profile=CapabilityExecutionProfile(side_effect=SideEffectType.READ),
            )
            restarted = make_engine(resumed_tool, state_store=restart_store)
            result = await restarted.engine.resume(plan.plan_id)
            saved = await restart_store.load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(resumed_tool.calls, 1)
            self.assertEqual(saved.state.status, PlanExecutionStatus.SUCCEEDED)
            self.assertEqual(saved.state.nodes["work"].attempt, running.state.nodes["work"].attempt)
            self.assertGreater(saved.state_version, running.state_version)

    async def test_running_non_idempotent_write_is_not_replayed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live_database = Path(directory) / "unsafe-live.db"
            restart_database = Path(directory) / "unsafe-restart.db"
            profile = CapabilityExecutionProfile(
                side_effect=SideEffectType.WRITE,
                idempotency=IdempotencyType.NONE,
            )
            first_tool = BlockingTool("restart.unsafe-write/v1", profile=profile)
            first = make_engine(first_tool, state_store=SQLiteStateStore(live_database))
            plan = ExecutionPlan(
                plan_id="restart-unsafe-write",
                nodes=(
                    PlanNode(
                        node_id="write",
                        capability="restart.unsafe-write/v1",
                    ),
                ),
            )

            execution = asyncio.create_task(first.engine.execute(make_request(), plan))
            await asyncio.wait_for(first_tool.started.wait(), timeout=1)
            running = await SQLiteStateStore(live_database).load(plan.plan_id)
            restart_store = SQLiteStateStore(restart_database)
            await restart_store.create(running)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution

            resumed_tool = ScriptedTool(
                "restart.unsafe-write/v1",
                (ResultEnvelope.success(ResultOutput(type="json", data={"ok": True})),),
                profile=profile,
            )
            restarted = make_engine(resumed_tool, state_store=restart_store)
            result = await restarted.engine.resume(plan.plan_id)
            saved = await restart_store.load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.FAILED)
            self.assertEqual(result.error.code, "HARNESS.PLAN.RESUME_UNSAFE")
            self.assertEqual(resumed_tool.calls, 0)
            self.assertEqual(saved.state.nodes["write"].status, NodeExecutionStatus.RUNNING)
            self.assertEqual(saved.state_version, running.state_version)

    async def test_async_waiting_completion_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "async-restart.db"
            accepted = ResultEnvelope.accepted(
                Continuation(job_ref="restart-job-42", waiting_reason="external_job")
            )
            first_async = ScriptedTool("restart.async/v1", (accepted,))
            first_echo = EchoTool("restart.echo/v1")
            first = make_engine(
                first_async,
                first_echo,
                state_store=SQLiteStateStore(database),
            )
            plan = ExecutionPlan(
                plan_id="restart-async-waiting",
                nodes=(
                    PlanNode(node_id="async", capability="restart.async/v1"),
                    PlanNode(
                        node_id="after",
                        capability="restart.echo/v1",
                        input_mapping={
                            "value": NodeOutputBinding(
                                node_id="async",
                                pointer="/output/data/value",
                            )
                        },
                    ),
                ),
                edges=(PlanEdge(from_node="async", to_node="after"),),
                outputs={
                    "value": NodeOutputBinding(
                        node_id="after",
                        pointer="/output/data/value",
                    )
                },
            )

            waiting = await first.engine.execute(make_request(), plan)
            self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
            self.assertEqual(waiting.continuation.job_ref, "restart-job-42")

            restarted_async = ScriptedTool("restart.async/v1", (accepted,))
            restarted_echo = EchoTool("restart.echo/v1")
            restarted = make_engine(
                restarted_async,
                restarted_echo,
                state_store=SQLiteStateStore(database),
            )
            result = await restarted.engine.complete_async_node(
                plan.plan_id,
                "async",
                ResultEnvelope.success(
                    ResultOutput(type="json", data={"value": "completed-after-restart"})
                ),
            )
            saved = await restarted.state_store.load(plan.plan_id)

            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.output.data["value"], "completed-after-restart")
            self.assertEqual(restarted_async.calls, 0)
            self.assertEqual(restarted_echo.calls, 1)
            self.assertEqual(saved.state.pending_jobs, [])
            self.assertEqual(saved.state.status, PlanExecutionStatus.SUCCEEDED)

    async def test_corrupted_sqlite_snapshot_fails_closed_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "corrupt.db"
            tool = ScriptedTool(
                "restart.corrupt/v1",
                (ResultEnvelope.success(ResultOutput(type="json", data={"ok": True})),),
            )
            store = SQLiteStateStore(database)
            fixture = make_engine(tool, state_store=store)
            plan = ExecutionPlan(
                plan_id="restart-corrupt-state",
                nodes=(PlanNode(node_id="work", capability="restart.corrupt/v1"),),
            )
            result = await fixture.engine.execute(make_request(), plan)
            self.assertEqual(result.status, ResultStatus.SUCCESS)

            # SQLite CHECK 仍要求合法 JSON，因此写入语法合法但不满足 Contract 的对象。
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE plan_execution_records SET payload_json = ? WHERE plan_id = ?",
                    ("{}", plan.plan_id),
                )
                connection.commit()

            restarted = make_engine(
                ScriptedTool(
                    "restart.corrupt/v1",
                    (ResultEnvelope.success(ResultOutput(type="json", data={"ok": True})),),
                ),
                state_store=SQLiteStateStore(database),
            )
            resumed = await restarted.engine.resume(plan.plan_id)

            self.assertEqual(resumed.status, ResultStatus.FAILED)
            self.assertEqual(resumed.error.code, "HARNESS.PLAN.STATE_LOAD_FAILED")


if __name__ == "__main__":
    unittest.main()
