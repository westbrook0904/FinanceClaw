"""Capability Provider 调用使用的最小重试协议。"""

from __future__ import annotations

from pydantic import Field, model_validator

from .base import ContractModel


class RetryPolicy(ContractModel):
    """单个 Capability Provider 的确定性指数退避参数。

    这是 Provider Fabric 的领域协议，不负责模型重试或图节点重试；前者交给
    LangChain，后者交给 LangGraph。
    """

    max_attempts: int = Field(default=1, ge=1)
    initial_backoff_ms: int = Field(default=100, ge=0)
    max_backoff_ms: int = Field(default=10_000, ge=0)
    multiplier: float = Field(default=2.0, ge=1.0)

    @model_validator(mode="after")
    def validate_backoff_range(self) -> RetryPolicy:
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms must be greater than or equal to initial_backoff_ms")
        return self
