"""Capability Provider 调用事件协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from harness_contracts import ContractModel
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from harness_contracts.context import _require_timezone
from pydantic import Field, field_validator, model_validator


class ExecutionEventName(StrEnum):
    PROVIDER_CANDIDATES = "provider.candidates"
    PROVIDER_SELECTED = "provider.selected"
    PROVIDER_RETRYING = "provider.retrying"
    PROVIDER_FALLBACK = "provider.fallback"
    PROVIDER_FAILED = "provider.failed"


class ExecutionEvent(ContractModel):
    """与 Provider 实例解耦、可供 Audit/Metrics/UI 消费的调用事实。"""

    event_id: NonEmptyString = Field(default_factory=lambda: uuid4().hex)
    name: ExecutionEventName
    request_id: NonEmptyString | None = None
    plan_id: NonEmptyString | None = None
    node_id: NonEmptyString | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state_version: int | None = Field(default=None, ge=1)
    trace_id: NonEmptyString | None = None
    attributes: FrozenJsonMapping = Field(default_factory=dict)

    _validate_timestamp = field_validator("timestamp")(_require_timezone)

    @model_validator(mode="after")
    def require_execution_reference(self) -> ExecutionEvent:
        if self.request_id is None and self.plan_id is None:
            raise ValueError("execution event requires request_id or plan_id")
        return self
