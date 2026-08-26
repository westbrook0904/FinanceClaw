"""Execution Event 发布/订阅 SPI。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ExecutionEvent


class EventPublisher(ABC):
    @abstractmethod
    async def publish(self, event: ExecutionEvent) -> None:
        """发布一个已经发生的执行事实。"""


class EventSubscriber(ABC):
    @abstractmethod
    async def on_event(self, event: ExecutionEvent) -> None:
        """消费一个执行事件。"""
