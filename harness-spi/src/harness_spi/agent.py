"""Agent 扩展接口。"""

from __future__ import annotations

from abc import abstractmethod

from harness_contracts import InvocationContext, ResultEnvelope

from .capability import Capability
from .models import AgentRequest


class AgentSPI(Capability):
    """可自主处理任务的 Agent Provider。"""

    @abstractmethod
    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        """在给定调用上下文中处理任务。"""

