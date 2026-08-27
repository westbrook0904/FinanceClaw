"""ModelProvider 专用的生成请求、结果、结构化输出与用量协议。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from harness_contracts import ContractModel, ErrorDetail
from harness_contracts.base import (
    FrozenJsonMapping,
    FrozenJsonValue,
    JsonValue,
    NonEmptyString,
)
from pydantic import Field, model_validator


class ModelRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelMessage(ContractModel):
    role: ModelRole
    content: NonEmptyString


class ModelResponseFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class ModelFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"


class ModelOutput(ContractModel):
    type: ModelResponseFormat
    data: FrozenJsonValue

    @model_validator(mode="after")
    def validate_data_shape(self) -> Self:
        if self.type is ModelResponseFormat.TEXT and not isinstance(self.data, str):
            raise ValueError("text model output must contain a string")
        if self.type is ModelResponseFormat.JSON and not isinstance(
            self.data, Mapping | tuple | list
        ):
            raise ValueError("json model output must contain an object or array")
        return self


class ModelUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class GenerateRequest(ContractModel):
    """一次非流式模型生成请求；``model`` 是 Registry 中的逻辑模型能力 ID。"""

    model: NonEmptyString
    messages: tuple[ModelMessage, ...] = Field(min_length=1)
    response_format: ModelResponseFormat = ModelResponseFormat.TEXT
    response_schema: FrozenJsonMapping | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_response_schema(self) -> Self:
        if (
            self.response_schema is not None
            and self.response_format is not ModelResponseFormat.JSON
        ):
            raise ValueError("response_schema requires json response_format")
        return self


class GenerateStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class GenerateResult(ContractModel):
    """ModelProvider/Gateway 的统一非流式结果。"""

    status: GenerateStatus
    output: ModelOutput | None = None
    usage: ModelUsage | None = None
    finish_reason: ModelFinishReason | None = None
    provider_id: NonEmptyString | None = None
    error: ErrorDetail | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)
    trace_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.status is GenerateStatus.SUCCESS:
            if self.output is None or self.usage is None or self.finish_reason is None:
                raise ValueError("successful generation requires output, usage, and finish_reason")
            if self.error is not None:
                raise ValueError("successful generation cannot contain error")
            return self
        if self.error is None:
            raise ValueError("failed generation requires error")
        if self.output is not None or self.usage is not None or self.finish_reason is not None:
            raise ValueError("failed generation cannot contain successful output fields")
        return self

    @classmethod
    def success(
        cls,
        output: ModelOutput,
        usage: ModelUsage,
        *,
        finish_reason: ModelFinishReason = ModelFinishReason.STOP,
        provider_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
        trace_id: str | None = None,
    ) -> GenerateResult:
        return cls(
            status=GenerateStatus.SUCCESS,
            output=output,
            usage=usage,
            finish_reason=finish_reason,
            provider_id=provider_id,
            metadata=metadata or {},
            trace_id=trace_id,
        )

    @classmethod
    def failure(
        cls,
        error: ErrorDetail,
        *,
        provider_id: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
        trace_id: str | None = None,
    ) -> GenerateResult:
        return cls(
            status=GenerateStatus.FAILED,
            error=error,
            provider_id=provider_id,
            metadata=metadata or {},
            trace_id=trace_id,
        )
