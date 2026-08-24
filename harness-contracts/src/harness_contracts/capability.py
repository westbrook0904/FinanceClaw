"""可调用能力的稳定描述协议。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, NonEmptyString


class CapabilityType(StrEnum):
    """阶段一支持的本地能力类型。"""

    AGENT = "agent"
    TOOL = "tool"


class CapabilityDescriptor(ContractModel):
    """Registry、Runtime 与 Plugin 共享的能力元数据。"""

    id: NonEmptyString
    name: NonEmptyString
    type: CapabilityType
    version: NonEmptyString
    input_schema: FrozenJsonMapping = Field(default_factory=dict)
    output_schema: FrozenJsonMapping = Field(default_factory=dict)
    tags: frozenset[str] = Field(default_factory=frozenset)
    metadata: FrozenJsonMapping = Field(default_factory=dict)
