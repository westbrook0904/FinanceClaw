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
            name=ExecutionEventName.PLAN_CREATED,
            plan_id="plan-1",
            state_version=1,
        )

        await bus.publish(event)

        self.assertEqual(bus.events(), (event,))
        self.assertEqual(collector.events, [event])

    async def test_noop_publisher_accepts_execution_event(self) -> None:
        publisher = NoOpEventPublisher()
        await publisher.publish(
            ExecutionEvent(name=ExecutionEventName.PLAN_STARTED, plan_id="plan-1")
        )
