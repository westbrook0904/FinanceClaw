"""FinanceClaw 公共错误分类、错误码和异常基类。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, JsonValue, NonEmptyString


class ErrorCategory(StrEnum):
    REQUEST = "request"
    APPLICATION = "application"
    POLICY = "policy"
    TIMEOUT = "timeout"


class ErrorCode(StrEnum):
    REQUEST_INVALID = "HARNESS.REQUEST.INVALID"
    APPLICATION_FAILED = "HARNESS.APPLICATION.FAILED"
    POLICY_DENIED = "HARNESS.POLICY.DENIED"

    TIMEOUT = "HARNESS.TIMEOUT"


class ErrorDetail(ContractModel):
    """Bounded structured error safe for retained domain boundaries."""

    code: NonEmptyString
    category: ErrorCategory
    message: NonEmptyString
    retryable: bool = False
    fallbackable: bool = False
    details: FrozenJsonMapping = Field(default_factory=dict)


class HarnessError(Exception):
    """FinanceClaw 内部异常的公共基类。"""

    default_code = ErrorCode.APPLICATION_FAILED
    default_category = ErrorCategory.APPLICATION
    default_retryable = False
    default_fallbackable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | ErrorCode | None = None,
        details: dict[str, JsonValue] | None = None,
        retryable: bool | None = None,
        fallbackable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = str(code or self.default_code)
        self.category = self.default_category
        self.details = details or {}
        self.retryable = self.default_retryable if retryable is None else retryable
        self.fallbackable = self.default_fallbackable if fallbackable is None else fallbackable

    def to_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            category=self.category,
            message=self.message,
            retryable=self.retryable,
            fallbackable=self.fallbackable,
            details=self.details,
        )


class RequestError(HarnessError):
    default_code = ErrorCode.REQUEST_INVALID
    default_category = ErrorCategory.REQUEST


class PolicyError(HarnessError):
    default_code = ErrorCode.POLICY_DENIED
    default_category = ErrorCategory.POLICY


class HarnessTimeoutError(HarnessError):
    default_code = ErrorCode.TIMEOUT
    default_category = ErrorCategory.TIMEOUT
    default_retryable = True
