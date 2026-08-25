"""Harness 的统一结果封装。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ContractModel, FrozenJsonMapping, FrozenJsonValue, JsonValue, NonEmptyString
from .errors import ErrorDetail


class ResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    ACCEPTED = "accepted"


class Continuation(ContractModel):
    """WAITING/异步执行可被后续调用重新定位的引用。"""

    plan_id: NonEmptyString | None = None
    node_id: NonEmptyString | None = None
    job_ref: NonEmptyString | None = None
    approval_id: NonEmptyString | None = None
    waiting_reason: NonEmptyString

    @model_validator(mode="after")
    def require_reference(self) -> Continuation:
        if not any((self.plan_id, self.node_id, self.job_ref, self.approval_id)):
            raise ValueError("continuation requires at least one reference")
        return self


class ResultIssue(ContractModel):
    source: NonEmptyString
    error: ErrorDetail
    metadata: FrozenJsonMapping = Field(default_factory=dict)


class ResultOutput(ContractModel):
    """成功调用的类型化输出。"""

    type: NonEmptyString
    data: FrozenJsonValue


class ResultEnvelope(ContractModel):
    """所有 Agent 与 Tool 最终归一化后的 Harness 结果。"""

    status: ResultStatus
    output: ResultOutput | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)
    error: ErrorDetail | None = None
    issues: tuple[ResultIssue, ...] = ()
    continuation: Continuation | None = None
    trace_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> ResultEnvelope:
        if self.status is ResultStatus.SUCCESS:
            if self.output is None:
                raise ValueError("successful result must include output")
            if self.error is not None:
                raise ValueError("successful result cannot include error")
            if self.issues or self.continuation is not None:
                raise ValueError("successful result cannot include issues or continuation")
            return self

        if self.status is ResultStatus.PARTIAL:
            if self.output is None:
                raise ValueError("partial result must include output")
            if not self.issues:
                raise ValueError("partial result must include at least one issue")
            if self.error is not None or self.continuation is not None:
                raise ValueError("partial result cannot include error or continuation")
            return self

        if self.status is ResultStatus.ACCEPTED:
            if self.continuation is None:
                raise ValueError("accepted result must include continuation")
            if self.output is not None or self.error is not None or self.issues:
                raise ValueError("accepted result cannot include final output, error, or issues")
            return self

        if self.status is ResultStatus.CANCELLED:
            if self.output is not None or self.continuation is not None:
                raise ValueError("cancelled result cannot include output or continuation")
            return self

        if self.error is None:
            raise ValueError("failed or denied result must include error")
        if self.output is not None or self.continuation is not None:
            raise ValueError("failed or denied result cannot include output or continuation")
        return self

    @classmethod
    def success(
        cls,
        output: ResultOutput,
        *,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.SUCCESS,
            output=output,
            trace_id=trace_id,
            metadata=metadata or {},
        )

    @classmethod
    def partial(
        cls,
        output: ResultOutput,
        issues: tuple[ResultIssue, ...] | list[ResultIssue],
        *,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.PARTIAL,
            output=output,
            issues=tuple(issues),
            trace_id=trace_id,
            metadata=metadata or {},
        )

    @classmethod
    def cancelled(
        cls,
        *,
        error: ErrorDetail | None = None,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.CANCELLED,
            error=error,
            trace_id=trace_id,
            metadata=metadata or {},
        )

    @classmethod
    def accepted(
        cls,
        continuation: Continuation,
        *,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.ACCEPTED,
            continuation=continuation,
            trace_id=trace_id,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        error: ErrorDetail,
        *,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.FAILED,
            error=error,
            trace_id=trace_id,
            metadata=metadata or {},
        )

    @classmethod
    def denied(
        cls,
        error: ErrorDetail,
        *,
        trace_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ResultEnvelope:
        return cls(
            status=ResultStatus.DENIED,
            error=error,
            trace_id=trace_id,
            metadata=metadata or {},
        )
