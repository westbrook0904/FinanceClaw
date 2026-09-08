"""PostgreSQL 上的真实进程硬退出、接管 CAS、全局和单租户容量竞争。"""

import asyncio
import json
import multiprocessing
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from financeclaw.coordination.application.coordinator import Coordinator
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.coordination.migration import LegacyMigration
from financeclaw.coordination.repository import CoordinatorRepository, now
from financeclaw.coordination.worker.__main__ import run_worker
from financeclaw.kernel.coordination import (
    BackendCapabilities,
    BackendExecutionRef,
    BackendObservation,
    SubmissionReceipt,
)
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationRow
from financeclaw.shared.execution_ledger.coordination_tables import CoordinatedRunRow
from financeclaw.shared.execution_ledger.cutover_tables import LegacyAdoptionRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.stage8.test_coordinator import admit
from tests.stage8.test_cutover import legacy, seal


class DurableBackend:
    """外部账本独立于 Worker 的进程内存，提交后崩溃不能抹掉远端事实。"""

    capabilities = BackendCapabilities(
        exact_observation=True,
        durable_continuation=True,
        recoverable_requests=True,
        operation_lookup=True,
        cancellation_confirmation=True,
        response_application_evidence=True,
    )

    def __init__(self, path, crash):
        """隔离文件只保存本测试的合成任务和操作摘要。"""
        self.path, self.crash = path, crash

    async def submit_task(self, command):
        """远端提交已持久化，本地还没有绑定回执时强制终止 OS 进程。"""
        import fcntl

        reference = BackendExecutionRef(
            backend_instance_id=command.backend_instance_id,
            operation_id=command.operation_id,
            task_id=command.task_id,
            execution_id="synthetic:" + command.operation_id,
        )
        with open(self.path, "a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            ledger = json.loads(stream.read() or '{"calls":0,"attempts":{}}')
            ledger["calls"] += 1
            ledger["attempts"][command.operation_id] = reference.model_dump(mode="json")
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(ledger))
            stream.flush()
            os.fsync(stream.fileno())
        if self.crash:
            os._exit(23)
        return SubmissionReceipt(status="submitted", execution_ref=reference)

    async def lookup_operation(self, command):
        """恢复者只能按原操作读取持久远端账本。"""
        import fcntl

        with open(self.path) as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            reference = json.load(stream)["attempts"].get(command.operation_id)
        return (
            SubmissionReceipt(status="submitted", execution_ref=reference)
            if reference
            else SubmissionReceipt(status="uncertain")
        )

    async def observe_execution(self, reference):
        """后台完成只返回原尝试的业务终态。"""
        return BackendObservation(
            execution_ref=reference,
            status="completed",
            result={"message": "synthetic durable completion"},
            evidence_ref="checkpoint",
        )


def coordinator_process(url, path, stop_process, crash):
    """新解释器独立装配正式 Worker 循环与连接池，不共享父进程对象。"""
    settings = FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        database_url=url,
        database_auto_create_schema=False,
        coordinator_enabled=True,
        coordinator_callback_url="http://localhost/internal/webhooks/langgraph-primary",
        coordinator_webhook_token="synthetic-isolated-process-test-0000",
        coordinator_poll_seconds=0.05,
        coordinator_reconcile_seconds=0.1,
        coordinator_lease_seconds=6,
        coordinator_worker_concurrency=1,
    )
    services = build_coordination(settings)
    coordinator = Coordinator(
        services.background_repository,
        services.background_releases,
        DurableBackend(path, crash),
        settings,
    )

    async def run():
        """外部信号触发有界排空，进程重启依赖 PostgreSQL 中的原责任。"""
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(coordinator, stop))
        try:
            while not stop_process.is_set() and not worker.done():
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await worker

    try:
        asyncio.run(run())
    finally:
        services.resources.database.close()


def competitor(url, action, payload, barrier, results):
    """竞争者通过独立 PostgreSQL 连接同步开始，返回结果不包含用户载荷。"""
    database = ApplicationDatabase(url)
    store = CoordinatorRepository(database.session_factory, backend_instance_id="langgraph-primary")
    try:
        barrier.wait(timeout=20)
        if action == "adopt":
            try:
                LegacyMigration(store, None, None)._adopt(payload, 2)
                results.put("adopted")
            except ExecutionConflict:
                results.put("fenced")
        else:
            claim = None
            deadline = time.monotonic() + 2
            while claim is None and time.monotonic() < deadline:
                claim = store.claim_due(
                    str(os.getpid()), lease_seconds=30, maximum_inflight=3, tenant_inflight=2
                )
                if claim is None:
                    time.sleep(0.02)
            results.put(claim)
    finally:
        database.close()


def postgres_url(setup):
    """并发一致性证据必须来自显式隔离的 PostgreSQL。"""
    url = setup.settings.database_url.get_secret_value()
    if not url.startswith("postgresql"):
        pytest.skip("explicit isolated PostgreSQL cluster required")
    return url


async def finish_processes(processes):
    """验收失败也清理自己的子进程，不操作用户既有服务。"""
    for process in processes:
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, timeout=5)


@pytest.mark.asyncio
async def test_coordinator_hard_exit_then_two_new_processes_do_not_resubmit(setup, tmp_path):
    """提交后硬退出，两个新进程恢复唯一回执；不增加预算、操作或最终消息。"""
    url = postgres_url(setup)
    _, accepted = await admit(setup)
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    path = str(tmp_path / "external-ledger.json")
    crashed = context.Process(target=coordinator_process, args=(url, path, stop, True))
    processes = [crashed]
    try:
        crashed.start()
        await asyncio.to_thread(crashed.join, timeout=20)
        assert crashed.exitcode == 23
        with setup.store.sessions.begin() as session:
            session.get(CoordinatedRunRow, accepted.run_id).lease_until = now() - timedelta(
                seconds=1
            )
        for _ in range(2):
            process = context.Process(target=coordinator_process, args=(url, path, stop, False))
            processes.append(process)
            process.start()
        for _ in range(300):
            with setup.store.sessions() as session:
                row = session.get(CoordinatedRunRow, accepted.run_id)
                if not row.active:
                    assert row.projection["status"] == "completed"
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("restarted coordinators did not settle original receipt")
        stop.set()
        for process in processes[1:]:
            await asyncio.to_thread(process.join, timeout=10)
            assert process.exitcode == 0
        with open(path) as stream:
            assert json.load(stream)["calls"] == 1
        assert setup.store.execution.get(accepted.run_id)["operation_calls"] == 1
        with setup.store.sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ConversationMessageRow)
                    .where(ConversationMessageRow.role == "assistant")
                )
                == 1
            )
    finally:
        stop.set()
        await finish_processes(processes)


@pytest.mark.asyncio
async def test_two_processes_cannot_both_adopt_same_legacy_root(setup):
    """两个操作员的同一 shadow 只有一个提交，旧驱动 CAS 随后必定失败。"""
    url = postgres_url(setup)
    old = await legacy(setup)
    seal(setup)
    plan = await old.migration.shadow(old.accepted.run_id)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=competitor, args=(url, "adopt", plan, barrier, results))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            await asyncio.to_thread(process.join, timeout=25)
            assert process.exitcode == 0
        assert sorted([results.get(timeout=5), results.get(timeout=5)]) == ["adopted", "fenced"]
        with setup.store.sessions() as session:
            assert session.scalar(select(func.count()).select_from(LegacyAdoptionRow)) == 1
        operation = next(iter(plan["commands"].values())).operation_id
        with pytest.raises(ExecutionConflict, match="fenced"):
            setup.store.execution.claim(operation, legacy=True)
        assert setup.store.execution.get(old.accepted.run_id)["operation_calls"] == 1
    finally:
        await finish_processes(processes)


@pytest.mark.asyncio
async def test_cross_process_capacity_limits_and_tenant_fairness(setup):
    """并发领取总数不超过 3、同租户不超过 2；第二租户仍能获得推进槽。"""
    url = postgres_url(setup)
    for tenant in ("first", "second"):
        for index in range(4):
            conversation = await setup.bff.create(tenant_id=tenant, subject_id="subject")
            await setup.bff.start_turn(
                conversation.conversation_id,
                ConversationTurnRequest(message="synthetic capacity request"),
                tenant_id=tenant,
                subject_id="subject",
                scopes=setup.scopes,
                idempotency_key=f"capacity-{tenant}-{index}",
            )
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(5), context.Queue()
    processes = [
        context.Process(target=competitor, args=(url, "claim", None, barrier, results))
        for _ in range(5)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            await asyncio.to_thread(process.join, timeout=25)
            assert process.exitcode == 0
        claims = [results.get(timeout=5) for _ in processes]
        assert len([claim for claim in claims if claim]) == 3
        with setup.store.sessions() as session:
            counts = dict(
                session.execute(
                    select(ConversationRow.tenant_id, func.count())
                    .join(
                        CoordinatedRunRow,
                        CoordinatedRunRow.conversation_id == ConversationRow.conversation_id,
                    )
                    .where(CoordinatedRunRow.lease_until > now())
                    .group_by(ConversationRow.tenant_id)
                ).all()
            )
        assert sorted(counts.values()) == [1, 2]
    finally:
        await finish_processes(processes)
