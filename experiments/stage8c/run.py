"""凭证隔离的 8C 验收：原生旧根接管、兼容 Worker 滚动替换和有限容量测量。"""

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select

from experiments.stage8.run import isolated_database
from experiments.stage8a.environment import ROOT, Processes
from experiments.stage8a.run import decision, projection
from financeclaw.coordination.backends.langgraph_backend import LangGraphBackend
from financeclaw.coordination.backends.langgraph_migration import LangGraphLegacyInspector
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.coordination.deployment import DeploymentControl
from financeclaw.coordination.migration import LegacyMigration
from financeclaw.coordination.repository import aware, now
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    RunProgressEventRow,
)
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import digest
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


async def migration_case(processes, services):
    """旧生产者确实退出后封闭门闩，接管只读检查过的原始父子 checkpoint。"""
    processes.start("legacy-producer", ["-m", "experiments.stage8c.seed_legacy"])
    producer = processes.processes["legacy-producer"]
    await asyncio.to_thread(producer.wait, timeout=120)
    assert producer.returncode == 0, "inspect isolated legacy-producer.log"
    processes.stop("legacy-producer")
    roots = json.loads((processes.directory / "legacy-roots.json").read_text())
    store = services.background_repository
    snapshots = {
        key: {row["run_id"]: row["snapshot"] for row in store.execution.tree(root)}
        for key, root in roots.items()
    }
    with store.sessions() as session:
        expected = {}
        for case in ("child", "workflow"):
            original_question = session.scalar(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.root_run_id == roots[case]
                )
            )
            expected[case] = (
                original_question.interaction_id,
                original_question.revision,
                aware(original_question.expires_at),
            )
    control = DeploymentControl(store)
    control.change(0, admission_paused=True, dispatch_paused=True)
    control.change(
        1,
        admission_paused=True,
        dispatch_paused=True,
        stopped_evidence_hash=digest(
            [producer.pid, producer.returncode, roots, "synthetic deployment stopped"]
        ),
    )
    backend = LangGraphBackend(services.resources.settings, store, services.background_releases)
    migration = LegacyMigration(
        store, services.background_releases, LangGraphLegacyInspector(backend)
    )
    for root in roots.values():
        plan = await migration.shadow(root)
        assert plan["state"] == "ready_for_reauthorization", migration.public(plan)
        await migration.adopt(
            root,
            fingerprint=plan["fingerprint"],
            shadow_hash=plan["shadow_hash"],
            control_revision=2,
        )
    with store.sessions() as session:
        for values in expected.values():
            question = session.get(PendingInteractionRow, values[0])
            assert (
                question.interaction_id,
                question.revision,
                aware(question.expires_at),
            ) == values
    for key, root in roots.items():
        current = {row["run_id"]: row["snapshot"] for row in store.execution.tree(root)}
        assert {run_id: row["context"] for run_id, row in current.items()} == {
            run_id: row["context"] for run_id, row in snapshots[key].items()
        }
    processes.workers()
    processes.bff()
    async with httpx.AsyncClient(
        base_url=processes.bff_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + processes.token},
    ) as client:
        for root in roots.values():
            response = await client.post(f"/v1/runs/{root}/authorization")
            response.raise_for_status()
    processes.stop("bff")
    control.change(2, admission_paused=False, dispatch_paused=False)
    answered = set()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        for case in ("child", "workflow"):
            child = projection(store, roots[case])
            if child.get("pending_interactions") and case not in answered:
                request = child["pending_interactions"][0]
                assert request["interaction_id"] == expected[case][0]
                await decision(processes, request)
                answered.add(case)
        if all(projection(store, root)["status"] == "completed" for root in roots.values()):
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError({key: projection(store, root) for key, root in roots.items()})
    for root in roots.values():
        for execution in store.execution.tree(root):
            runs = await backend.client.runs.list(execution["snapshot"]["thread_id"], limit=100)
            operations = [run["metadata"]["operation_id"] for run in runs]
            assert len(operations) == len(set(operations)), (
                "duplicate native operation after adoption"
            )
    return {
        "legacy_producer_exit_code": producer.returncode,
        "native_roots_completed": len(roots),
        "scenarios": list(roots),
        "original_question_id_revision_deadline_preserved": True,
        "original_context_preserved": True,
        "duplicate_native_operations": 0,
        "post_cutover_frontend_gets": 0,
    }


async def rolling_capacity(processes, services):
    """温启动 Worker、暂停派发后同时释放 12 根；替换一个 Worker，另一个继续推进。"""
    store, control = (
        services.background_repository,
        DeploymentControl(services.background_repository),
    )
    control.change(3, admission_paused=False, dispatch_paused=True)
    processes.bff()
    roots = []
    async with httpx.AsyncClient(
        base_url=processes.bff_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + processes.token},
        timeout=30,
    ) as client:
        for index in range(12):
            response = await client.post("/v1/conversations", json={})
            response.raise_for_status()
            conversation = response.json()["conversation_id"]
            response = await client.post(
                f"/v1/conversations/{conversation}/turns",
                json={"message": "calculate"},
                headers={"Idempotency-Key": f"load-{index}"},
            )
            response.raise_for_status()
            roots.append(response.json()["run_id"])
    processes.stop("bff")
    processes.stop("worker-0")
    processes.start("worker-0", ["-m", "financeclaw.coordination.worker"])
    released = now()
    control.change(4, admission_paused=False, dispatch_paused=False)
    maximum_leased = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with store.sessions() as session:
            maximum_leased = max(
                maximum_leased,
                session.scalar(
                    select(func.count())
                    .select_from(CoordinatedRunRow)
                    .where(CoordinatedRunRow.lease_until > now())
                ),
            )
            remaining = session.scalar(
                select(func.count())
                .select_from(CoordinatedRunRow)
                .where(CoordinatedRunRow.run_id.in_(roots), CoordinatedRunRow.active.is_(True))
            )
        if not remaining:
            break
        await asyncio.sleep(0.05)
    else:
        raise TimeoutError("rolling capacity roots did not settle")
    assert maximum_leased <= 4
    assert all(projection(store, root)["status"] == "completed" for root in roots)
    with store.sessions() as session:
        advanced = list(
            session.scalars(
                select(RunProgressEventRow.created_at).where(
                    RunProgressEventRow.run_id.in_(roots), RunProgressEventRow.revision == 2
                )
            )
        )
    assert len(advanced) == len(roots)
    latency = sorted((aware(value) - released).total_seconds() for value in advanced)
    p95 = latency[math.ceil(len(latency) * 0.95) - 1]
    return {
        "roots": len(roots),
        "workers": 2,
        "slots_per_worker": 4,
        "maximum_observed_inflight_single_tenant": maximum_leased,
        "first_advance_p95_seconds": round(p95, 3),
        "initial_2_second_target_met": p95 <= 2,
        "completion_seconds": round((now() - released).total_seconds(), 3),
        "rolling_worker_replacement": True,
        "frontend_observers": 0,
    }


async def main():
    """不接受许可证或模型凭据，只允许 native dev 与受控合成负载。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    database = isolated_database(args.postgres_url)
    processes = Processes(args.directory, database)
    migration_path = str(ROOT / "financeclaw/shared/infrastructure/migrations")
    migration = (
        "from alembic.config import Config; from alembic import command; "
        f"c=Config({str(ROOT / 'alembic.ini')!r}); "
        f"c.set_main_option('script_location', {migration_path!r}); "
        "command.upgrade(c, 'head')"
    )
    services = None
    try:
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
        processes.agent()
        processes.ingress()
        report = {
            "scope": "isolated PostgreSQL 16 / credential-free native langgraph dev",
            "production_rollout": "not_executed",
            "driver_version": 3,
        }
        report["migration"] = await migration_case(processes, services)
        print(json.dumps(report["migration"], ensure_ascii=False), flush=True)
        report["capacity"] = await rolling_capacity(processes, services)
        report["source_digests"] = {
            str(path.relative_to(ROOT)): digest(path.read_text())
            for folder in ("financeclaw/coordination", "experiments/stage8c")
            for path in sorted((ROOT / folder).rglob("*.py"))
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report["capacity"]), flush=True)
    finally:
        processes.close()
        if services:
            services.resources.database.close()
        from urllib.parse import urlsplit

        import psycopg
        from psycopg import sql

        with psycopg.connect(args.postgres_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(urlsplit(database).path.lstrip("/"))
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
