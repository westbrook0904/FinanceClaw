"""Feishu ingress transport. Only the unified API admits business decisions."""

import asyncio
from dataclasses import asdict

import httpx


class RemoteFeishuService:
    """Forward normalized channel events to the sole product API."""

    def __init__(self, settings):
        """Inject dependencies without starting background work."""
        self.client = httpx.AsyncClient(
            base_url=settings.internal_api_url,
            headers={
                "Authorization": f"Bearer {settings.integration_service_token.get_secret_value()}"
            },
            timeout=settings.native_timeout_seconds,
        )
        self._tasks = set()
        self._capacity = settings.feishu_max_concurrency
        self._slots = asyncio.Semaphore(self._capacity)
        self.card_actions = self

    def submit(self, message, gateway):
        """Admit channel work only while bounded local ingress capacity remains available."""
        if len(self._tasks) >= self._capacity:
            return None
        task = asyncio.create_task(self.process(message, gateway))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def process(self, message, gateway):
        """Await API admission, then deliver only its immediate channel replies."""
        async with self._slots:
            response = await self.client.post(
                "/internal/channels/feishu/events",
                json={"kind": "message", "message": asdict(message)},
            )
            response.raise_for_status()
            body = response.json()
            for reply in body["replies"]:
                await gateway.send_text(**reply)
            return body["status"]

    async def handle(self, raw):
        """Forward a verified card callback and return the committed API acknowledgement."""
        response = await self.client.post(
            "/internal/channels/feishu/events", json={"kind": "card", "event": raw}
        )
        response.raise_for_status()
        return response.json()

    async def shutdown(self):
        """Drain pending channel admission before closing its HTTP client."""
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()
