"""阶段二 in-process EventPublisher 参考实现。"""

from __future__ import annotations

from .models import ExecutionEvent
from .publisher import EventPublisher, EventSubscriber


class NoOpEventPublisher(EventPublisher):
    async def publish(self, event: ExecutionEvent) -> None:
        if not isinstance(event, ExecutionEvent):
            raise TypeError("event must be ExecutionEvent")


class InMemoryEventBus(EventPublisher):
    """按订阅顺序同步分发，并保留不可变事件快照供测试/观察。"""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._events: list[ExecutionEvent] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        if not isinstance(subscriber, EventSubscriber):
            raise TypeError("subscriber must implement EventSubscriber")
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    @property
    def subscribers(self) -> tuple[EventSubscriber, ...]:
        return tuple(self._subscribers)

    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    async def publish(self, event: ExecutionEvent) -> None:
        if not isinstance(event, ExecutionEvent):
            raise TypeError("event must be ExecutionEvent")
        snapshot = ExecutionEvent.model_validate(event.model_dump(mode="json"))
        self._events.append(snapshot)
        for subscriber in tuple(self._subscribers):
            await subscriber.on_event(snapshot)
