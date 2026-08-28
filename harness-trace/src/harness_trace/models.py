"""Trace、Span 与 Event 的阶段一稳定模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from harness_contracts import ContractModel
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from pydantic import Field, model_validator


class SpanType(StrEnum):
    """稳定 Harness Span 类型；瞬时状态继续使用 Event/Attribute。"""

    REQUEST = "request"
    RUNTIME = "runtime"
    POLICY = "policy"
    REGISTRY_RESOLVE = "registry_resolve"
    PROVIDER_SELECT = "provider_select"
    CAPABILITY = "capability"
    MODEL = "model"
    AGENT = "agent"
    TOOL = "tool"
    PLAN = "plan"
    PLAN_NODE = "plan_node"
    SCHEDULER = "scheduler"
    ROUTE = "route"
    PLANNER = "planner"


class SpanStatus(StrEnum):
    """Span 生命周期的最小状态集合。"""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


class TraceError(ContractModel):
    """Trace 中可安全序列化的错误摘要。"""

    type: NonEmptyString
    message: NonEmptyString
    code: NonEmptyString | None = None


class Span(ContractModel):
    """一次可观测执行区间的不可变快照。"""

    trace_id: NonEmptyString
    span_id: NonEmptyString
    parent_span_id: NonEmptyString | None = None
    type: SpanType
    name: NonEmptyString
    start_time: datetime
    end_time: datetime | None = None
    attributes: FrozenJsonMapping = Field(default_factory=dict)
    status: SpanStatus = SpanStatus.RUNNING
    error: TraceError | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Span:
        for value, field_name in (
            (self.start_time, "start_time"),
            (self.end_time, "end_time"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} must include timezone information")

        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time must not be earlier than start_time")

        if self.status is SpanStatus.RUNNING:
            if self.end_time is not None:
                raise ValueError("running span cannot include end_time")
            if self.error is not None:
                raise ValueError("running span cannot include error")
            return self

        if self.end_time is None:
            raise ValueError("finished span must include end_time")
        if self.status is SpanStatus.ERROR and self.error is None:
            raise ValueError("error span must include error")
        if self.status is not SpanStatus.ERROR and self.error is not None:
            raise ValueError("only error span may include error")
        return self


class TraceEvent(ContractModel):
    """挂载在某个 Span 上的时间点事件。"""

    trace_id: NonEmptyString
    span_id: NonEmptyString
    name: NonEmptyString
    timestamp: datetime
    attributes: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamp(self) -> TraceEvent:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")
        return self
