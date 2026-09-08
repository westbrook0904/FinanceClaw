"""验证正式 8A 服务：真实 PostgreSQL、两个 OS Worker、原生 LangGraph 与 BFF 退出。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import httpx
from sqlalchemy import func, select

from experiments.stage8.run import isolated_database
from experiments.stage8a.environment import ROOT, Processes
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.shared.execution_ledger.coordination_tables import (
    BackendAttemptRow,
    ContinuationRow,
    CoordinatedRunRow,
    CoordinationInboxRow,
)
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.repository import digest
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


async def accepted(processes, case):
    """仅通过真实 BFF 的用户写入口受理，随后完全结束 BFF。"""
    processes.bff()
    headers = {"Authorization": "Bearer " + processes.token, "Idempotency-Key": case}
    message = {
        "root": "calculate",
        "workflow_approve": (
            '/workflow portfolio_review {"portfolio_name":"synthetic",'
            '"positions":[{"symbol":"AAPL","quantity":1,"cost_basis":1}]}'
        ),
        "workflow_reject": (
            '/workflow portfolio_review {"portfolio_name":"synthetic",'
            '"positions":[{"symbol":"AAPL","quantity":1,"cost_basis":1}]}'
        ),
    }.get(case, "/agent market_research_agent synthetic research")
    async with httpx.AsyncClient(
        base_url=processes.bff_url, headers=headers, trust_env=False
    ) as client:
        response = await client.post("/v1/conversations", json={})
        response.raise_for_status()
        conversation = response.json()["conversation_id"]
        path = f"/v1/conversations/{conversation}/turns"
        responses = await asyncio.gather(
            *[client.post(path, json={"message": message}) for _ in range(2)]
        )
        for response in responses:
            response.raise_for_status()
        assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
        result = responses[0].json()
    processes.stop("bff")
    return result


async def decision(processes, request, *, reject=False):
    """用户交互才临时重启 BFF，POST 决定后再次退出；不用 GET 或 SSE。"""
    processes.bff()
    response = {"revision": request["revision"], "kind": request["kind"]}
    if request["kind"] == "approval":
        response.update(
            decision="reject" if reject else "approve", action_hash=request["action_hash"]
        )
    else:
        response["answer"] = {"analysis_period": "synthetic period"}
    async with httpx.AsyncClient(
        base_url=processes.bff_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + processes.token, "Idempotency-Key": "user-decision"},
    ) as client:
        result = await client.post(request["response_url"], json=response)
        result.raise_for_status()
    processes.stop("bff")


def projection(store, root_id):
    """验收观察仅直接读取 DB，不经过任何产品查询或 Worker 入口。"""
    with store.sessions() as session:
        return dict(session.get(CoordinatedRunRow, root_id).projection)


async def cases(processes, services, selected=None):
    """断开所有观察者、丢全部回调和回执、重启 Worker，仍保持原操作与 Journal。"""
    store = services.background_repository
    report = {}
    for case in selected or (
        "root",
        "child_question",
        "lost_callbacks",
        "lost_receipts",
        "worker_restart",
        "cancel",
        "workflow_approve",
        "workflow_reject",
    ):
        for i in range(2):
            processes.stop(f"worker-{i}")
        if case == "lost_callbacks":
            processes.stop("ingress")
        elif "ingress" not in processes.processes:
            processes.ingress()
        accepted_turn = await accepted(processes, case)
        root_id = accepted_turn["run_id"]
        with store.sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(BackendAttemptRow)
                    .where(BackendAttemptRow.run_id == root_id)
                )
                == 0
            )
        processes.workers(lose_receipts=case == "lost_receipts")
        started, answered = time.monotonic(), False
        while time.monotonic() - started < 100:
            for name, process in processes.processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{case}: {name} exited")
            state = projection(store, root_id)
            if state["status"] in {"completed", "failed", "cancelled"}:
                break
            if state.get("pending_interactions") and not answered:
                request = state["pending_interactions"][0]
                assert request["owner_run_id"] != root_id
                if case == "worker_restart":
                    for i in range(2):
                        processes.stop(f"worker-{i}", kill=True)
                    processes.workers()
                if case == "cancel":
                    processes.bff()
                    async with httpx.AsyncClient(
                        base_url=processes.bff_url,
                        trust_env=False,
                        headers={"Authorization": "Bearer " + processes.token},
                    ) as client:
                        result = await client.post(f"/v1/runs/{root_id}/cancel")
                        result.raise_for_status()
                    processes.stop("bff")
                else:
                    await decision(processes, request, reject=case == "workflow_reject")
                answered = True
            if (
                state.get("waiting_reason")
                in {
                    "coordination_requires_attention",
                    "unsupported_interruption",
                    "submission_uncertain",
                }
                and case != "lost_receipts"
            ):
                raise AssertionError((case, state["waiting_reason"], root_id))
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError((case, state, root_id))
        assert state["status"] == ("cancelled" if case == "cancel" else "completed"), (case, state)
        if case != "root":
            assert answered, (case, "expected a real child interaction")
        records = store.journal.list_messages(accepted_turn["conversation_id"])
        assert len(records) == (1 if case == "cancel" else 2)
        with store.sessions() as session:
            operations = list(
                session.scalars(
                    select(RunOperationRow)
                    .join(RunExecutionRow)
                    .where(RunExecutionRow.root_run_id == root_id)
                )
            )
            attempts = list(
                session.scalars(
                    select(BackendAttemptRow).where(BackendAttemptRow.run_id == root_id)
                )
            )
            continuations = list(
                session.scalars(select(ContinuationRow).where(ContinuationRow.run_id == root_id))
            )
            delegations = list(
                session.scalars(select(DelegationRow).where(DelegationRow.parent_run_id == root_id))
            )
            callback_count = session.scalar(
                select(func.count())
                .select_from(CoordinationInboxRow)
                .where(
                    CoordinationInboxRow.run_id == root_id,
                    CoordinationInboxRow.kind == "backend_notification",
                )
            )
            assert len(attempts) == len(operations)
            if case not in {"root", "cancel"}:
                assert len(delegations) == 1 and delegations[0].delivered_at
                assert all(row.applied_operation_id for row in continuations)
            if case == "workflow_reject":
                assert delegations[0].execution_status == "rejected"
            if case == "lost_callbacks":
                assert callback_count == 0
            report[case] = {
                "status": state["status"],
                "operations": len(operations),
                "backend_attempts": len(attempts),
                "callbacks_persisted": callback_count,
                "journal_messages": len(records),
                "applied_continuations": sum(
                    bool(row.applied_operation_id) for row in continuations
                ),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
        # backend metadata 再独立计数，不能只靠本地唯一约束宣称没有重复远端 Run。
        for execution in store.execution.tree(root_id):
            thread_id = execution["snapshot"]["thread_id"]
            async with httpx.AsyncClient(base_url=processes.agent_url, trust_env=False) as client:
                response = await client.get(f"/threads/{thread_id}/runs", params={"limit": 100})
                response.raise_for_status()
                native_ops = [r.get("metadata", {}).get("operation_id") for r in response.json()]
                assert len(native_ops) == len(set(native_ops)), (case, "duplicate native operation")
        print(json.dumps({"case": case, **report[case]}, ensure_ascii=False), flush=True)
    return report


async def main():
    """只为明确的本机实验集群创建新库；证据保留计数／摘要，临时日志保留在指定目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--agent-image")
    parser.add_argument("--license-env", type=Path)
    parser.add_argument("--redis-url", default="redis://host.docker.internal:56389")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    database = isolated_database(args.postgres_url)
    if args.agent_image:
        from experiments.stage8a.docker_environment import DockerProcesses

        processes = DockerProcesses(
            args.directory,
            database,
            image=args.agent_image,
            postgres_url=args.postgres_url,
            redis_url=args.redis_url,
            license_env=args.license_env,
        )
    else:
        processes = Processes(args.directory, database)
    migration_path = str(ROOT / "financeclaw/shared/infrastructure/migrations")
    migration = (
        "from alembic.config import Config; from alembic import command; "
        f"c=Config({str(ROOT / 'alembic.ini')!r}); "
        f"c.set_main_option('script_location', {migration_path!r}); "
        "command.upgrade(c, 'head')"
    )
    with (args.directory / "migration.log").open("w") as log:
        subprocess.run(
            [sys.executable, "-c", migration],
            cwd=args.directory,
            env=processes.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    os.environ.update(
        {key: value for key, value in processes.env.items() if key.startswith("FINANCECLAW_")}
    )
    services = build_coordination(FinanceClawSettings(_env_file=None))
    report = {
        "business_database": "PostgreSQL 16",
        "worker_processes": 2,
        "agent_runtime": args.agent_image or "langgraph dev / runtime-inmem",
        "frontend_observers": 0,
        "versions": {
            name: version(name)
            for name in ("langgraph-api", "langgraph", "langgraph-sdk", "sqlalchemy", "psycopg")
        },
    }
    try:
        processes.agent()
        processes.ingress()
        report["cases"] = await cases(processes, services, args.cases)
        report["source_digests"] = {
            str(path.relative_to(ROOT)): digest(path.read_text())
            for folder in ("financeclaw/coordination", "experiments/stage8a")
            for path in sorted((ROOT / folder).rglob("*.py"))
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        processes.close()
        services.resources.database.close()


if __name__ == "__main__":
    asyncio.run(main())
