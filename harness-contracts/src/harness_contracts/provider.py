"""Provider 身份、健康状态和执行尝试的稳定协议。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, FrozenJsonMapping, NonEmptyString
from .context import _require_timezone


class ProviderHealthStatus(StrEnum):
    """Provider 的最小运行时健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ProviderAttemptStatus(StrEnum):
    """一次 Provider 调用尝试的最小生命周期状态。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProviderDescriptor(ContractModel):
    """Provider 级身份与部署元数据。

    Capability 的 side-effect、egress 和 idempotency 语义仍由
    CapabilityDescriptor.execution_profile 唯一描述，避免同一 Capability
    因 Provider 不同产生两套执行语义。
    """

    provider_id: NonEmptyString
    capability_id: NonEmptyString
    plugin_id: NonEmptyString
    implementation_version: NonEmptyString
    priority: int = 0
    tags: frozenset[NonEmptyString] = Field(default_factory=frozenset)
    region: NonEmptyString | None = None
    tenant_visibility: frozenset[NonEmptyString] = Field(default_factory=frozenset)
    equivalence_group: NonEmptyString | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)


class ProviderHealthSnapshot(ContractModel):
    """某个 HealthSource 在特定时刻观察到的 Provider 健康快照。"""

    provider_id: NonEmptyString
    status: ProviderHealthStatus
    observed_at: datetime
    source: NonEmptyString
    reason_code: NonEmptyString | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    _validate_observed_at = field_validator("observed_at")(_require_timezone)


class ProviderPin(ContractModel):
    """受控场景下要求精确 Provider 的显式选择约束。"""

    provider_id: NonEmptyString
    reason: NonEmptyString | None = None


class ProviderAttempt(ContractModel):
    """一次已选 Provider 上的具体调用尝试，可用于 Trace/Checkpoint/Replay。"""

    provider_id: NonEmptyString
    selection_key: NonEmptyString
    provider_attempt: int = Field(ge=1)
    retry_attempt: int = Field(ge=1)
    equivalence_group: NonEmptyString | None = None
    started_at: datetime
    completed_at: datetime | None = None
    status: ProviderAttemptStatus = ProviderAttemptStatus.RUNNING
    failure_code: NonEmptyString | None = None

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        return self
