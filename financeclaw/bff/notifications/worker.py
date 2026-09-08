"""可独立重启的事件订阅与发送循环：不装配 BFF、Agent 或 Coordinator。"""

import asyncio
import logging
import signal
from contextlib import suppress
from uuid import uuid4

from financeclaw.bff.notifications.feishu import FeishuNotificationGateway, Receipt
from financeclaw.bff.notifications.repository import NotificationRepository, StaleSender
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.observability.logging import configure_json_logging
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.notifications.facts import require_schema

LOGGER = logging.getLogger(__name__)


async def deliver(repository, gateway, claim, settings):
    """发送前做两层有效性检查，提交 sending 后的异常只记为 uncertain。"""
    try:
        async with asyncio.timeout(settings.notification_timeout_seconds):
            rejection = await gateway.check_target(claim["address"])
    except Exception:
        rejection = Receipt("retry", error_class="target_check_unavailable")
    if rejection is not None:
        await asyncio.to_thread(
            repository.settle, claim, rejection, max_failures=settings.notification_max_failures
        )
        return
    ready = await asyncio.to_thread(
        repository.prepare,
        claim,
        recovery_seconds=settings.notification_verified_dedup_seconds,
        timeout_seconds=settings.notification_timeout_seconds,
        recovery_evidence=settings.notification_dedup_evidence,
    )
    if not ready:
        return
    try:
        async with asyncio.timeout(settings.notification_timeout_seconds):
            receipt = await gateway.send(claim)
    except Exception:
        receipt = Receipt("uncertain", error_class="reply_response_lost")
    await asyncio.to_thread(
        repository.settle, claim, receipt, max_failures=settings.notification_max_failures
    )


async def run_worker(repository, gateway, settings, stop, *, worker_id=None):
    """单槽有界投递，独立心跳和续租；SIGTERM 排空当前有限调用。"""
    worker_id = worker_id or "notification-" + uuid4().hex
    while not stop.is_set():
        claim = None
        task = None
        try:
            await asyncio.to_thread(repository.heartbeat, worker_id)
            await asyncio.to_thread(repository.materialize)
            claim = await asyncio.to_thread(
                repository.claim, worker_id, lease_seconds=settings.notification_lease_seconds
            )
            if claim:
                task = asyncio.create_task(deliver(repository, gateway, claim, settings))
                while not task.done():
                    done, _ = await asyncio.wait(
                        {task}, timeout=settings.notification_lease_seconds / 3
                    )
                    if not done:
                        await asyncio.to_thread(repository.heartbeat, worker_id)
                        if not await asyncio.to_thread(
                            repository.renew,
                            claim,
                            lease_seconds=settings.notification_lease_seconds,
                        ):
                            task.cancel()
                            break
                with suppress(asyncio.CancelledError):
                    await task
                continue
        except StaleSender:
            LOGGER.info("Discarded stale notification receipt")
        except Exception as exc:
            LOGGER.warning(
                "Notification worker step failed", extra={"error_type": type(exc).__name__}
            )
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.notification_poll_seconds)


async def main():
    """独立角色入口；迁移和发送开关都必须显式就绪。"""
    settings = FinanceClawSettings()
    if not settings.feishu_notifications_enabled:
        raise RuntimeError("notification sender is disabled")
    configure_json_logging(settings.log_level)
    database = ApplicationDatabase(settings.database_url.get_secret_value())
    try:
        require_schema(database.session_factory)
        repository = NotificationRepository(
            database.session_factory,
            app_id=settings.feishu_app_id,
            allowed_open_ids=settings.feishu_allowed_open_ids,
        )
        gateway = FeishuNotificationGateway.from_settings(settings)
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        await run_worker(repository, gateway, settings, stop)
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
