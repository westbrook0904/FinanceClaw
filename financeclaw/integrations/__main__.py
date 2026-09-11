"""Run channel connectivity and delivery consumers, independently of API and native workers."""

import asyncio
import signal

from langgraph_sdk import get_client

from financeclaw.integrations.health import HEALTH_PATH, heartbeat
from financeclaw.integrations.history import consume
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.runtime import configure_observability
from financeclaw.shared.infrastructure.security.egress import EgressPolicy
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


async def main():
    """Run channel connectivity and durable delivery consumers in the integrations role."""
    settings = FinanceClawSettings()
    if settings.process_role != "integrations" or settings.integration_service_token is None:
        raise RuntimeError("integrations role and service credential are required")
    EgressPolicy(
        settings.internal_service_hosts, require_https=False, allow_private_hosts=True
    ).validate(settings.internal_api_url)
    telemetry = configure_observability(settings)
    resources = build_resources(settings, enable_persistence=True)
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    client = get_client(
        timeout=30,
        url=settings.internal_api_url,
        api_key=None,
        headers={
            "Authorization": "Bearer " + settings.integration_service_token.get_secret_value()
        },
    )
    channel, notifications, health, waiter = None, None, None, None
    consumer = asyncio.create_task(consume(resources, client.store, stop))
    try:
        if settings.feishu_enabled:
            from financeclaw.integrations.feishu.channel import FeishuChannelAdapter
            from financeclaw.integrations.feishu.client import RemoteFeishuService
            from financeclaw.integrations.notifications.repository import NotificationRepository
            from financeclaw.integrations.notifications.worker import NotificationWorker

            channel = FeishuChannelAdapter(
                RemoteFeishuService(settings),
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret.get_secret_value(),
                allowed_open_ids=settings.feishu_allowed_open_ids,
                max_concurrency=settings.feishu_max_concurrency,
                security_mode=settings.feishu_security_mode,
                connect_timeout_seconds=settings.feishu_connect_timeout_seconds,
            )
            notifications = NotificationWorker(
                NotificationRepository(
                    resources.database.session_factory,
                    app_id=settings.feishu_app_id,
                    allowed_open_ids=settings.feishu_allowed_open_ids,
                ),
                settings,
            )
            await notifications.start()
            await channel.start()
        health = asyncio.create_task(
            heartbeat(consumer, stop, channel=channel, notifications=notifications)
        )
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({waiter, consumer}, return_when=asyncio.FIRST_COMPLETED)
        if consumer in done:
            await (
                consumer
            )  # A dead consumer must fail the role, not leave a healthy-looking process.
    finally:
        stop.set()
        if channel:
            await channel.stop()
        if notifications:
            await notifications.stop()
        tasks = [task for task in (consumer, health, waiter) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
        HEALTH_PATH.unlink(missing_ok=True)
        resources.database.close()
        telemetry.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
