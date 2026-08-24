"""Harness 的统一结果封装。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ContractModel, FrozenJsonMapping, FrozenJsonValue, JsonValue, NonEmptyString
from .errors import ErrorDetail


class ResultStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"


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
    trace_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> ResultEnvelope:
        if self.status is ResultStatus.SUCCESS:
            if self.output is None:
                raise ValueError("successful result must include output")
            if self.error is not None:
                raise ValueError("successful result cannot include error")
            return self

        if self.error is None:
            raise ValueError("failed or denied result must include error")
        if self.output is not None:
            raise ValueError("failed or denied result cannot include output")
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
