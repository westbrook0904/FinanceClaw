"""一条命令复现 Stage-8.0：共享事务、LangGraph 回调和 Coordination 基础推进。"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import func, select

from experiments.stage8.backend import ProbeBackend
from experiments.stage8.driver import TERMINAL
from experiments.stage8.environment import WebhookSink, agent_server
from experiments.stage8.processes import WorkerProcesses
from experiments.stage8.store import Inbox, ProbeStore
from experiments.stage8.transaction_probe import transaction_probes
from experiments.stage8.webhook_probe import probe_outbound_filter, probe_webhooks
from financeclaw.kernel.coordination import BackendExecutionRef, BackendNotification


def isolated_database(base: str) -> str:
    """只在明确的本机实验集群新建唯一数据库，不清空任何既有数据库。"""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(base)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("stage8 probe accepts only an isolated localhost database")
    name = "financeclaw_stage8_" + uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return urlunsplit(parsed._replace(path="/" + name))


async def probe_coordination(store, database, backend, sink) -> dict:
    """两个独立 OS Worker 验证基础推进和恢复，不运行 BFF／SSE。"""
    workers = WorkerProcesses(database, backend.native._url, sink.url, str(backend.directory))
    report = {"worker_processes": 2, "cases": {}}
    await workers.start()
    try:
        for case in (
            "normal",
            "duplicate_late",
            "lost_callbacks",
            "lost_receipts",
            "worker_restart",
            "cancel",
            "authorization_expired",
        ):
            sink.drop = case == "lost_callbacks"
            offset = len(sink.events)
            started = time.monotonic()
            conversation = store.create_conversation()
            root_id = store.admit(
                conversation,
                ttl=0 if case == "authorization_expired" else 120,
                lose_receipt=case == "lost_receipts",
            )
            answered = False
            async with asyncio.timeout(60):
                while True:
                    if any(not process.is_alive() for process in workers.processes):
                        raise RuntimeError("coordination worker died")
                    state = store.read(root_id)
                    stage = state["payload"]["stage"]
                    if stage in TERMINAL:
                        break
                    if stage == "user_wait" and not answered:
                        if case == "worker_restart":
                            workers.kill()
                            await workers.start()
                        store.decision(root_id, cancel=case == "cancel")
                        answered = True
                    if case == "duplicate_late" and len(sink.events) > offset:
                        notification = BackendNotification.model_validate(
                            sink.events[offset]["notification"]
                        )
                        store.notification(notification)
                        store.notification(notification)
                    await asyncio.sleep(0.03)
            expected = (
                "authorization_expired"
                if case == "authorization_expired"
                else "cancelled"
                if case == "cancel"
                else "completed"
            )
            assert stage == expected, (case, state)
            executions = store.execution.tree(root_id)
            operations = [
                operation
                for execution in executions
                for operation in store.execution.operations_for_run(execution["run_id"])
            ]
            if expected == "completed":
                assert len(executions) == 2 and len(operations) == 4
                assert state["payload"]["child_status"] == "completed"
                assert state["payload"]["delivery_status"] == "applied"
                assert len(store.journal.list_messages(conversation)) == 2
                for execution in executions:
                    refs = [
                        operation["server_run_id"]
                        for operation in operations
                        if operation["run_id"] == execution["run_id"]
                    ]
                    thread_id, _ = json.loads(refs[0])
                    native_runs = await backend.client.runs.list(thread_id)
                    assert len(native_runs) == 2, "a remote command was resubmitted"
                if case == "normal":
                    original = BackendExecutionRef.model_validate(
                        state["payload"]["parent_reference"]
                    )
                    historical = await backend.observe(original)
                    assert historical.status == "waiting"
                    assert (
                        historical.requests[0].model_dump(mode="json")
                        == state["payload"]["delegation"]
                    )
                    report["old_attempt_checkpoint_is_not_latest_thread_state"] = True
                    roles = {
                        "delegation_interrupt": "start:" + root_id,
                        "user_interrupt": "start:" + state["payload"]["child_id"],
                        "child_resume_completed": "answer:interaction:" + root_id,
                        "parent_resume_completed": "delivery:request:" + root_id,
                    }
                    coverage = {}
                    async with asyncio.timeout(5):
                        for role, operation_id in roles.items():
                            reference = next(
                                operation["server_run_id"]
                                for operation in operations
                                if operation["operation_id"] == operation_id
                            )
                            while not any(
                                event["notification"]["execution_id"] == reference
                                for event in sink.events[offset:]
                            ):
                                await asyncio.sleep(0.03)
                            coverage[role] = next(
                                event["notification"]["status_hint"]
                                for event in sink.events[offset:]
                                if event["notification"]["execution_id"] == reference
                            )
                    report["exact_attempt_webhook_coverage"] = coverage
                if case == "duplicate_late":
                    notification = BackendNotification.model_validate(
                        sink.events[offset]["notification"]
                    )
                    store.notification(notification.model_copy(update={"payload_digest": "f" * 64}))
                    await asyncio.sleep(0.15)
                    assert store.read(root_id)["revision"] == state["revision"]
            else:
                assert len(store.journal.list_messages(conversation)) == 1
                if expected == "authorization_expired":
                    assert all(operation["status"] == "prepared" for operation in operations)
                    assert executions[0]["operation_calls"] == 0
            statuses = sorted(
                {event["notification"]["status_hint"] for event in sink.events[offset:]}
            )
            report["cases"][case] = {
                "passed": True,
                "terminal": stage,
                "business_operations": len(operations),
                "callback_statuses": statuses,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            print(f"coordination: {case} passed", flush=True)
        # A crash after the unique claim but before a recoverable receipt leaves responsibility
        # unresolved. Neither an empty lookup nor cancellation authorizes a new submission.
        conversation = store.create_conversation()
        root_id = store.admit(conversation, unresolved_receipt=True)
        await asyncio.sleep(0.8)
        store.decision(root_id, cancel=True)
        async with asyncio.timeout(10):
            while store.read(root_id)["payload"].get("blocked_reason") != "submission_unresolved":
                await asyncio.sleep(0.05)
        operation = store.execution.operations_for_run(root_id)[0]
        assert operation["status"] == "uncertain" and operation["server_run_id"] is None
        assert store.read(root_id)["payload"]["stage"] not in TERMINAL
        assert len(store.journal.list_messages(conversation)) == 1
        report["unresolved_receipt_not_resent_or_falsely_cancelled"] = True
    finally:
        sink.drop = False
        await workers.close()
    return report


async def run(args, output: Path) -> None:
    """形成机器可读证据，任一断言失败即非零退出，不输出伪完成结果。"""
    database = isolated_database(args.database)
    store = ProbeStore(database)
    store.initialize()
    report = {
        "versions": {
            name: version(name)
            for name in (
                "langgraph",
                "langgraph-api",
                "langgraph-sdk",
                "sqlalchemy",
                "psycopg",
            )
        }
    }
    with store.sessions() as session:
        from sqlalchemy import text

        report["versions"]["postgresql"] = session.scalar(text("SHOW server_version"))
    # Use subprocesses before starting HTTP listener threads for the transaction race probes.
    report["shared_transactions"] = transaction_probes(store, database)
    print("PostgreSQL shared transactions and fencing passed", flush=True)
    sink = WebhookSink()
    sink.start()
    try:
        report["outbound_filter"] = await probe_outbound_filter(output / "filter-config", sink)
        with agent_server(output / "agent-server", sink) as url:
            backend = ProbeBackend(url, sink.url, output / "bindings")
            report["webhooks"] = await probe_webhooks(backend, sink)
            print("LangGraph webhook probes passed", flush=True)
            sink.on_event = store.notification
            report["coordination"] = await probe_coordination(store, database, backend, sink)
            with store.sessions() as session:
                report["pending_early_notifications"] = session.scalar(
                    select(func.count()).select_from(Inbox).where(Inbox.run_id.is_(None))
                )
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"Verified report: {output / 'report.json'}", flush=True)
    finally:
        sink.close()
        store.db.close()


def main() -> None:
    """仅接受显式实验服务；默认端口与项目本地服务分离。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "STAGE8_DATABASE_URL", "postgresql://postgres@127.0.0.1:55438/postgres"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="financeclaw-stage8-evidence-"))
    output.mkdir(parents=True, exist_ok=True)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    print(f"Stage-8 evidence directory: {output}", flush=True)
    asyncio.run(run(args, output))


if __name__ == "__main__":
    main()
