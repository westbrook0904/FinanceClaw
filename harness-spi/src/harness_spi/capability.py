"""所有本地 Capability Provider 共享的最小发现接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import CapabilityDescriptor


class Capability(ABC):
    """Registry 可发现的能力。

    本接口只统一描述语义；执行入口由 AgentSPI 和 ToolSPI 分别定义。
    """

    @abstractmethod
    def descriptor(self) -> CapabilityDescriptor:
        """返回稳定、无副作用的能力描述。"""
