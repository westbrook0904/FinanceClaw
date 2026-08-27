"""阶段二最小执行事件协议。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from harness_contracts import ContractModel
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from harness_contracts.context import _require_timezone
from pydantic import Field, field_validator, model_validator


class ExecutionEventName(StrEnum):
    PLAN_CREATED = "plan.created"
    PLAN_STARTED = "plan.started"
    PLAN_WAITING = "plan.waiting"
    PLAN_RESUMED = "plan.resumed"
    PLAN_COMPLETED = "plan.completed"
    PLAN_FAILED = "plan.failed"
    PLAN_CANCELLED = "plan.cancelled"
    NODE_READY = "node.ready"
    NODE_STARTED = "node.started"
    NODE_RETRYING = "node.retrying"
    NODE_WAITING = "node.waiting"
    NODE_RESUMED = "node.resumed"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_DENIED = "node.denied"
    NODE_CANCELLED = "node.cancelled"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    ASYNC_ACCEPTED = "async.accepted"
    ASYNC_COMPLETED = "async.completed"
    CHECKPOINT_SAVED = "checkpoint.saved"
    PROVIDER_CANDIDATES = "provider.candidates"
    PROVIDER_SELECTED = "provider.selected"
    PROVIDER_RETRYING = "provider.retrying"
    PROVIDER_FALLBACK = "provider.fallback"
    PROVIDER_FAILED = "provider.failed"


class ExecutionEvent(ContractModel):
    """与业务 Provider 解耦、可供 Audit/Metrics/UI 消费的执行事实。"""

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
