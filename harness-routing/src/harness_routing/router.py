"""Router 的无执行权 SPI。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import RouteDecision

from .models import RoutingContext


class Router(ABC):
    """只产生 RouteDecision、不执行 Capability 或 Plan 的路由接口。"""

    @property
    @abstractmethod
    def router_id(self) -> str:
        """返回用于配置和可观测性的稳定 Router ID。"""

    @abstractmethod
    async def route(self, context: RoutingContext) -> RouteDecision:
        """根据受限上下文产生一个结构化路由决策。"""
