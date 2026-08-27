"""ModelProvider SPI。"""

from __future__ import annotations

from abc import abstractmethod

from harness_contracts import InvocationContext
from harness_spi import Capability

from .contracts import GenerateRequest, GenerateResult


class ModelProvider(Capability):
    """使用模型原生生成协议、同时可被共享 Provider Registry 发现的 Provider。"""

    @abstractmethod
    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        """执行一次非流式生成。"""
