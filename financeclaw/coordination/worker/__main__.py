"""python -m financeclaw.coordination.worker 独立启动；不依赖任何 BFF app。"""

import asyncio
import logging
import signal
from uuid import uuid4

from financeclaw.coordination.bootstrap import build_coordinator
from financeclaw.coordination.repository import StaleCoordinator
from financeclaw.shared.execution_ledger.repository import ExecutionConflict

LOGGER = logging.getLogger(__name__)


async def _run_slot(coordinator, stop: asyncio.Event, *, worker_id):
    """一个并发槽执行有限步进；每次只领取一根，租约与预算跨进程共享。"""
    store, settings = coordinator.store, coordinator.settings
    worker_id = worker_id or str(uuid4())
    last_heartbeat = 0.0
    try:
        while not stop.is_set():
            try:
                if asyncio.get_running_loop().time() - last_heartbeat >= 2:
                    await asyncio.to_thread(store.heartbeat, worker_id)
                    last_heartbeat = asyncio.get_running_loop().time()
                claim = await asyncio.to_thread(
                    store.claim_due,
                    worker_id,
                    lease_seconds=settings.coordinator_lease_seconds,
                    maximum_inflight=settings.coordinator_max_inflight,
                    tenant_inflight=settings.coordinator_tenant_inflight,
                )
            except Exception as exc:
                LOGGER.warning("coordinator database unavailable (%s)", type(exc).__name__)
                claim = None
            if claim is None:
                try:
                    await asyncio.wait_for(stop.wait(), settings.coordinator_poll_seconds)
                except TimeoutError:
                    pass
                continue

            async def renew(current_claim=claim):
                """远程等待期间使用独立短事务续租，不持有根业务锁。"""
                while True:
                    await asyncio.sleep(min(2, settings.coordinator_lease_seconds / 3))
                    if not await asyncio.to_thread(
                        store.renew, current_claim, lease_seconds=settings.coordinator_lease_seconds
                    ):
                        raise StaleCoordinator("lease renewal failed")
                    await asyncio.to_thread(store.heartbeat, worker_id)

            task = asyncio.create_task(coordinator.advance(claim))
            renewal = asyncio.create_task(renew())
            delay, error = settings.coordinator_reconcile_seconds, None
            try:
                done, _ = await asyncio.wait(
                    {task, renewal},
                    timeout=settings.coordinator_max_step_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if renewal in done:
                    await renewal
                if task not in done:
                    raise TimeoutError("coordinator step timed out")
                delay = await task
            except StaleCoordinator:
                continue
            except Exception as exc:
                error = type(exc).__name__
                # 不输出可能带原始响应、身份或模型数据的异常文本。
                LOGGER.warning(
                    "coordinator step deferred (%s)",
                    error,
                    extra={"run_id": claim["run_id"], "error_type": error},
                )
                if isinstance(exc, (ExecutionConflict, PermissionError, ValueError)):
                    try:
                        await asyncio.to_thread(
                            coordinator.blocked, claim, "coordination_requires_attention"
                        )
                    except StaleCoordinator:
                        continue
                delay = min(60, max(1, delay * 2))
            finally:
                for pending in (task, renewal):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(task, renewal, return_exceptions=True)
            try:
                await asyncio.to_thread(store.finish, claim, delay=delay, error=error)
            except StaleCoordinator:
                pass
            except Exception as exc:
                # 数据库在当前步之后失联时保留原租约／操作事实，后续按原回执恢复。
                LOGGER.warning("coordinator finish deferred (%s)", type(exc).__name__)
    finally:
        try:
            await asyncio.to_thread(store.heartbeat, worker_id, remove=True)
        except Exception as exc:
            LOGGER.warning("coordinator heartbeat cleanup deferred (%s)", type(exc).__name__)


async def run_worker(coordinator, stop: asyncio.Event, *, worker_id=None):
    """有界并发槽共享停止信号；SIGTERM 后停止领取并等待各步有界收尾。"""
    worker_id = worker_id or str(uuid4())
    tasks = [
        asyncio.create_task(_run_slot(coordinator, stop, worker_id=f"{worker_id}:{index}"))
        for index in range(coordinator.settings.coordinator_worker_concurrency)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    """SIGTERM 关闭新领取，已领取的一步在有界超时内保存状态。"""
    services, coordinator = build_coordinator()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run_worker(coordinator, stop)
    finally:
        services.resources.database.close()


if __name__ == "__main__":
    asyncio.run(main())
