"""PostgreSQL 上独立 Sender 进程崩溃与竞争；外部渠道使用持久合成账本。"""

import asyncio
import json
import multiprocessing
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from financeclaw.bff.notifications.feishu import Receipt
from financeclaw.bff.notifications.repository import NotificationRepository
from financeclaw.bff.notifications.worker import run_worker
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.notifications.tables import NotificationDeliveryRow
from tests.stage8.test_notifications import completed


class DurableChannel:
    """模拟渠道独立事实，进程硬退出不会抹掉已成功的消息或固定 UUID。"""

    def __init__(self, path, crash):
        """同一文件由跨进程锁保护，合成载荷不包含用户资料或凭证。"""
        self.path, self.crash = path, crash

    async def check_target(self, address):
        """合成测试目标保持有效。"""
        return None

    async def send(self, claim):
        """先提交渠道端记录，再模拟发送者未存本地回执就硬退出。"""
        import fcntl

        with open(self.path, "a+") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            data = json.loads(file.read() or '{"attempts": [], "messages": {}}')
            data["attempts"].append(
                {"key": claim["send_key"], "content": claim["content"], "address": claim["address"]}
            )
            data["messages"].setdefault(claim["send_key"], "synthetic-receipt")
            file.seek(0)
            file.truncate()
            file.write(json.dumps(data))
            file.flush()
            os.fsync(file.fileno())
        if self.crash:
            os._exit(23)
        return Receipt("sent", message_id="synthetic-receipt")


def sender_process(url, path, stop_process, crash):
    """新解释器装配真正的独立发送循环，不共享父进程数据库连接或内存任务。"""
    database = ApplicationDatabase(url)
    repository = NotificationRepository(
        database.session_factory, app_id="app", allowed_open_ids=frozenset({"user"})
    )
    settings = SimpleNamespace(
        notification_timeout_seconds=2,
        notification_lease_seconds=15,
        notification_poll_seconds=0.05,
        notification_max_failures=5,
        notification_verified_dedup_seconds=120,
        notification_dedup_evidence="synthetic-ledger-window",
    )

    async def run():
        """外部停止只触发优雅排空，不截断发送调用。"""
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(repository, DurableChannel(path, crash), settings, stop)
        )
        while not stop_process.is_set() and not task.done():
            await asyncio.sleep(0.05)
        stop.set()
        await task

    try:
        asyncio.run(run())
    finally:
        database.close()


@pytest.mark.asyncio
async def test_sender_hard_exit_and_two_process_takeover(setup, tmp_path):
    """已送达后硬退出；两个新进程只能恢复原分片，唯一外部消息和 Journal 不变。"""
    if not setup.settings.database_url.get_secret_value().startswith("postgresql"):
        pytest.skip("explicit isolated PostgreSQL cluster required")
    _, _, _, repository = await completed(setup)
    repository.materialize()
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    ledger = str(tmp_path / "synthetic-channel.json")
    url = setup.settings.database_url.get_secret_value()
    crashed = context.Process(target=sender_process, args=(url, ledger, stop, True))
    children = [crashed]
    try:
        crashed.start()
        await asyncio.to_thread(crashed.join, timeout=15)
        assert crashed.exitcode == 23
        with repository.sessions.begin() as session:
            row = session.scalar(select(NotificationDeliveryRow))
            assert row.status == "sending" and row.message_id is None
            row.lease_until = now() - timedelta(seconds=1)
        for _ in range(2):
            child = context.Process(target=sender_process, args=(url, ledger, stop, False))
            children.append(child)
            child.start()
        for _ in range(200):
            with repository.sessions() as session:
                if session.scalar(select(NotificationDeliveryRow.status)) == "sent":
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("restarted senders did not settle original receipt")
        stop.set()
        for child in children[1:]:
            await asyncio.to_thread(child.join, timeout=10)
            assert child.exitcode == 0
        with open(ledger) as file:
            data = json.load(file)
        assert len(data["messages"]) == 1 and len(data["attempts"]) == 2
        assert data["attempts"][0] == data["attempts"][1]
        assert setup.backend.calls == 1
    finally:
        stop.set()
        for child in children:
            if child.is_alive():
                child.terminate()
            await asyncio.to_thread(child.join, timeout=5)
