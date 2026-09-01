"""可调用能力的稳定描述协议。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, NonEmptyString


class CapabilityType(StrEnum):
    """阶段一支持的本地能力类型。"""

    AGENT = "agent"
    TOOL = "tool"
    MODEL = "model"


class SideEffectType(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class EgressType(StrEnum):
    NONE = "none"
    INTERNAL = "internal"
    EXTERNAL = "external"


class IdempotencyType(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class CapabilityCompletionMode(StrEnum):
    """Capability outbound 是否在当前调用内同步终结。"""

    UNKNOWN = "unknown"
    SYNC = "sync"
    ASYNC = "async"


class CapabilityExecutionProfile(ContractModel):
    """Scheduler 判断重试安全性所需的通用能力语义。"""

    side_effect: SideEffectType = SideEffectType.NONE
    egress: EgressType = EgressType.NONE
    idempotency: IdempotencyType = IdempotencyType.NONE
    completion_mode: CapabilityCompletionMode = CapabilityCompletionMode.UNKNOWN


class CapabilityDescriptor(ContractModel):
    """Registry、Runtime 与 Plugin 共享的能力元数据。"""

    id: NonEmptyString
    name: NonEmptyString
    type: CapabilityType
    version: NonEmptyString
    input_schema: FrozenJsonMapping = Field(default_factory=dict)
    output_schema: FrozenJsonMapping = Field(default_factory=dict)
    execution_profile: CapabilityExecutionProfile = Field(
        default_factory=CapabilityExecutionProfile
    )
    tags: frozenset[str] = Field(default_factory=frozenset)
    metadata: FrozenJsonMapping = Field(default_factory=dict)
