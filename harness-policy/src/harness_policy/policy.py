"""Retained Memory policy extension interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import PolicyContext, PolicyDecision, PolicyPhase


class Policy(ABC):
    """一个可声明适用阶段的独立治理规则。"""

    @property
    def name(self) -> str:
        return type(self).__name__

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        """Apply to each retained memory boundary unless narrowed by a policy."""

        return frozenset(PolicyPhase)

    @abstractmethod
    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """对当前治理边界做单一策略判断。"""
