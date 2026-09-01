"""ModelProvider SPI。"""

from __future__ import annotations

from abc import abstractmethod

from harness_contracts import (
    ErrorCode,
    InvocationContext,
    ModelProviderFeatures,
    ProviderError,
    StructuredOutputSpec,
)
from harness_spi import Capability

from .contracts import GenerateRequest, GenerateResult
from .preparation import PreparedStructuredOutput


class ModelProvider(Capability):
    """使用模型原生生成协议、同时可被共享 Provider Registry 发现的 Provider。"""

    @abstractmethod
    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        """执行一次非流式生成。"""

    @property
    def features(self) -> ModelProviderFeatures:
        """Legacy Provider 默认不具备 strict structured output 能力。"""

        return ModelProviderFeatures()

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput | None:
        """纯本地、零网络、无损编译具体 Schema；legacy 默认不支持。"""

        return None

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        """执行已无损编译的 strict 请求；禁止默认降级调用 ``generate``。"""

        raise ProviderError(
            "model provider does not support prepared structured output",
            code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
        )
