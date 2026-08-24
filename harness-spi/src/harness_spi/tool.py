"""Tool 扩展接口。"""

from __future__ import annotations

from abc import abstractmethod

from harness_contracts import InvocationContext, ResultEnvelope

from .capability import Capability
from .models import ToolRequest


class ToolSPI(Capability):
    """输入明确、单步且倾向确定性的 Tool Provider。"""

    @abstractmethod
    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        """在给定调用上下文中执行工具操作。"""

