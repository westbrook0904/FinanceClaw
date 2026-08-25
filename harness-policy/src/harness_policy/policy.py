"""Policy Provider 的最小扩展接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import PolicyContext, PolicyDecision


class Policy(ABC):
    """调用边界上的单一策略检查。"""

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """对一次调用边界做单一策略判断。"""
