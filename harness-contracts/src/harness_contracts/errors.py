"""FinanceClaw 公共错误分类、错误码和异常基类。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, JsonValue, NonEmptyString


class ErrorCategory(StrEnum):
    REQUEST = "request"
    APPLICATION = "application"
    POLICY = "policy"
    CONTEXT = "context"
    MEMORY = "memory"
    TIMEOUT = "timeout"


class ErrorCode(StrEnum):
    REQUEST_INVALID = "HARNESS.REQUEST.INVALID"
    APPLICATION_FAILED = "HARNESS.APPLICATION.FAILED"
    POLICY_DENIED = "HARNESS.POLICY.DENIED"

    CONTEXT_INVALID = "HARNESS.CONTEXT.INVALID"
    CONTEXT_POLICY_UNSUPPORTED = "HARNESS.CONTEXT.POLICY_UNSUPPORTED"
    CONTEXT_PROJECTION_REQUIRED = "HARNESS.CONTEXT.PROJECTION_REQUIRED"

    MEMORY_INVALID = "HARNESS.MEMORY.INVALID"
    MEMORY_TRUSTED_SCOPE_REQUIRED = "HARNESS.MEMORY.TRUSTED_SCOPE_REQUIRED"
    MEMORY_SCOPE_VIOLATION = "HARNESS.MEMORY.SCOPE_VIOLATION"
    MEMORY_NAMESPACE_NOT_ALLOWED = "HARNESS.MEMORY.NAMESPACE_NOT_ALLOWED"
    MEMORY_POLICY_DENIED = "HARNESS.MEMORY.POLICY_DENIED"
    MEMORY_POLICY_UNSUPPORTED = "HARNESS.MEMORY.POLICY_UNSUPPORTED"
    MEMORY_EVIDENCE_INVALID = "HARNESS.MEMORY.EVIDENCE_INVALID"
    MEMORY_TOO_LARGE = "HARNESS.MEMORY.TOO_LARGE"
    MEMORY_PROPOSAL_CONFLICT = "HARNESS.MEMORY.PROPOSAL_CONFLICT"
    MEMORY_PROVIDER_FAILED = "HARNESS.MEMORY.PROVIDER_FAILED"
    MEMORY_PROVIDER_INVALID = "HARNESS.MEMORY.PROVIDER_INVALID"

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


class ContextError(HarnessError):
    default_code = ErrorCode.CONTEXT_INVALID
    default_category = ErrorCategory.CONTEXT


class MemoryAccessError(HarnessError):
    default_code = ErrorCode.MEMORY_INVALID
    default_category = ErrorCategory.MEMORY


class HarnessTimeoutError(HarnessError):
    default_code = ErrorCode.TIMEOUT
    default_category = ErrorCategory.TIMEOUT
    default_retryable = True
