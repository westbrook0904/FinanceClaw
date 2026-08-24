"""Harness 执行环境与可变执行状态协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator

from .base import ContractModel, FrozenJsonMapping, JsonValue, MutableContractModel, NonEmptyString
from .request import Request


def _require_timezone(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must include timezone information")
    return value


class IdentityContext(ContractModel):
    """通过认证后注入的调用主体，不直接信任 Request 中的用户信息。"""

    subject: NonEmptyString
    scopes: frozenset[str] = Field(default_factory=frozenset)
    attributes: FrozenJsonMapping = Field(default_factory=dict)


class TenantContext(ContractModel):
    """经过 Runtime 解析后的租户上下文。"""

    tenant_id: NonEmptyString
    attributes: FrozenJsonMapping = Field(default_factory=dict)


class TraceContext(ContractModel):
    """跨模块传播的最小追踪标识。"""

    trace_id: NonEmptyString
    span_id: NonEmptyString | None = None
    parent_span_id: NonEmptyString | None = None
    baggage: FrozenJsonMapping = Field(default_factory=dict)


class CancellationContext(ContractModel):
    """取消状态的只读快照；具体取消信号由 Runtime 管理。"""

    cancelled: bool = False
    reason: str | None = None
    requested_at: datetime | None = None

    _validate_requested_at = field_validator("requested_at")(_require_timezone)


class InvocationContext(ContractModel):
    """一次 Invocation 中由 Harness 构造并只读传播的执行环境。"""

    request: Request
    identity: IdentityContext | None = None
    tenant: TenantContext | None = None
    deadline_at: datetime | None = None
    attributes: FrozenJsonMapping = Field(default_factory=dict)
    trace_context: TraceContext | None = None
    cancellation: CancellationContext = Field(default_factory=CancellationContext)

    _validate_deadline = field_validator("deadline_at")(_require_timezone)


class ExecutionStatus(StrEnum):
    """一次 Invocation 的最小状态集合。"""

    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionState(MutableContractModel):
    """与只读 Context 分离的本次执行可变状态。"""

    status: ExecutionStatus = ExecutionStatus.CREATED
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    _validate_started_at = field_validator("started_at")(_require_timezone)
    _validate_completed_at = field_validator("completed_at")(_require_timezone)
