"""harness-events 最小 in-process EventPublisher 测试。"""

from __future__ import annotations

import unittest

from harness_events import (
    EventSubscriber,
    ExecutionEvent,
    ExecutionEventName,
    InMemoryEventBus,
    NoOpEventPublisher,
)


class Collector(EventSubscriber):
    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    async def on_event(self, event: ExecutionEvent) -> None:
        self.events.append(event)


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_bus_keeps_snapshot_and_notifies_subscriber(self) -> None:
        bus = InMemoryEventBus()
        collector = Collector()
        bus.subscribe(collector)
        event = ExecutionEvent(
            name=ExecutionEventName.PROVIDER_SELECTED,
            request_id="request-1",
        )

        await bus.publish(event)

        self.assertEqual(bus.events(), (event,))
        self.assertEqual(collector.events, [event])

    async def test_noop_publisher_accepts_execution_event(self) -> None:
        publisher = NoOpEventPublisher()
        await publisher.publish(
            ExecutionEvent(
                name=ExecutionEventName.PROVIDER_CANDIDATES,
                request_id="request-1",
            )
        )

    async def test_provider_event_can_use_request_reference_without_plan(self) -> None:
        event = ExecutionEvent(
            name=ExecutionEventName.PROVIDER_SELECTED,
            request_id="request-1",
            attributes={"provider_id": "provider-a"},
        )

        self.assertEqual(event.request_id, "request-1")
        self.assertIsNone(event.plan_id)

    async def test_execution_event_requires_request_or_plan_reference(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionEvent(name=ExecutionEventName.PROVIDER_FAILED)
