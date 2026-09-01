"""Harness 公共错误分类、错误码和异常基类。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, JsonValue, NonEmptyString


class ErrorCategory(StrEnum):
    REQUEST = "request"
    REGISTRY = "registry"
    POLICY = "policy"
    PLUGIN = "plugin"
    CAPABILITY = "capability"
    PROVIDER = "provider"
    SELECTION = "selection"
    ROUTE = "route"
    CONTEXT = "context"
    PLANNER = "planner"
    TIMEOUT = "timeout"


class ErrorCode(StrEnum):
    REQUEST_INVALID = "HARNESS.REQUEST.INVALID"
    REQUEST_TARGET_REQUIRED = "HARNESS.REQUEST.TARGET_REQUIRED"
    REQUEST_MODE_CONFLICT = "HARNESS.REQUEST.MODE_CONFLICT"
    REGISTRY_NOT_FOUND = "HARNESS.REGISTRY.NOT_FOUND"
    POLICY_DENIED = "HARNESS.POLICY.DENIED"
    PLUGIN_LOAD_FAILED = "HARNESS.PLUGIN.LOAD_FAILED"
    CAPABILITY_EXECUTION_FAILED = "HARNESS.CAPABILITY.EXECUTION_FAILED"

    PROVIDER_NOT_FOUND = "HARNESS.PROVIDER.NOT_FOUND"
    PROVIDER_DUPLICATE = "HARNESS.PROVIDER.DUPLICATE"
    PROVIDER_CAPABILITY_MISMATCH = "HARNESS.PROVIDER.CAPABILITY_MISMATCH"
    PROVIDER_NO_ELIGIBLE_CANDIDATE = "HARNESS.PROVIDER.NO_ELIGIBLE_CANDIDATE"
    PROVIDER_PIN_NOT_ALLOWED = "HARNESS.PROVIDER.PIN_NOT_ALLOWED"
    PROVIDER_PIN_NOT_FOUND = "HARNESS.PROVIDER.PIN_NOT_FOUND"
    PROVIDER_FALLBACK_UNSAFE = "HARNESS.PROVIDER.FALLBACK_UNSAFE"
    PROVIDER_SELECTION_FAILED = "HARNESS.PROVIDER.SELECTION_FAILED"
    PROVIDER_HEALTH_UNAVAILABLE = "HARNESS.PROVIDER.HEALTH_UNAVAILABLE"
    PROVIDER_EXECUTION_FAILED = "HARNESS.PROVIDER.EXECUTION_FAILED"
    PROVIDER_RESUME_UNSAFE = "HARNESS.PROVIDER.RESUME_UNSAFE"

    SELECTION_INVALID_CONTEXT = "HARNESS.SELECTION.INVALID_CONTEXT"
    SELECTION_INVALID_DECISION = "HARNESS.SELECTION.INVALID_DECISION"

    ROUTE_NO_MATCH = "HARNESS.ROUTE.NO_MATCH"
    ROUTE_INVALID_DECISION = "HARNESS.ROUTE.INVALID_DECISION"
    ROUTE_MODE_NOT_ALLOWED = "HARNESS.ROUTE.MODE_NOT_ALLOWED"
    ROUTE_MODE_NOT_AVAILABLE = "HARNESS.ROUTE.MODE_NOT_AVAILABLE"
    ROUTE_CAPABILITY_NOT_ALLOWED = "HARNESS.ROUTE.CAPABILITY_NOT_ALLOWED"
    ROUTE_PLANNER_NOT_ALLOWED = "HARNESS.ROUTE.PLANNER_NOT_ALLOWED"
    ROUTE_MODEL_FAILED = "HARNESS.ROUTE.MODEL_FAILED"
    ROUTE_APPROVAL_NOT_SUPPORTED = "HARNESS.ROUTE.APPROVAL_NOT_SUPPORTED"

    CONTEXT_INVALID = "HARNESS.CONTEXT.INVALID"
    CONTEXT_POLICY_UNSUPPORTED = "HARNESS.CONTEXT.POLICY_UNSUPPORTED"
    CONTEXT_PROJECTION_REQUIRED = "HARNESS.CONTEXT.PROJECTION_REQUIRED"

    PLANNER_NOT_CONFIGURED = "HARNESS.PLANNER.NOT_CONFIGURED"
    PLANNER_NOT_APPLICABLE = "HARNESS.PLANNER.NOT_APPLICABLE"
    PLANNER_INVALID_OUTPUT = "HARNESS.PLANNER.INVALID_OUTPUT"
    PLANNER_PLAN_TOO_LARGE = "HARNESS.PLANNER.PLAN_TOO_LARGE"
    PLANNER_REPAIR_EXHAUSTED = "HARNESS.PLANNER.REPAIR_EXHAUSTED"
    PLANNER_DEADLINE_EXCEEDED = "HARNESS.PLANNER.DEADLINE_EXCEEDED"
    PLANNER_MODEL_FAILED = "HARNESS.PLANNER.MODEL_FAILED"

    PLAN_IDENTITY_GENERATION_FAILED = "HARNESS.PLAN.IDENTITY_GENERATION_FAILED"
    PLAN_TEMPLATE_INVALID = "HARNESS.PLAN.TEMPLATE_INVALID"
    PLAN_EXECUTION_ID_CONFLICT = "HARNESS.PLAN.EXECUTION_ID_CONFLICT"

    MODEL_STRUCTURED_OUTPUT_UNSUPPORTED = "HARNESS.MODEL.STRUCTURED_OUTPUT_UNSUPPORTED"
    MODEL_STRUCTURED_OUTPUT_SCHEMA_INVALID = "HARNESS.MODEL.STRUCTURED_OUTPUT_SCHEMA_INVALID"
    MODEL_STRUCTURED_OUTPUT_INVALID = "HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID"
    MODEL_STRUCTURED_OUTPUT_TRUNCATED = "HARNESS.MODEL.STRUCTURED_OUTPUT_TRUNCATED"
    MODEL_REFUSED = "HARNESS.MODEL.REFUSED"
    MODEL_CONTENT_FILTERED = "HARNESS.MODEL.CONTENT_FILTERED"
    MODEL_ACCOUNTING_INCOMPLETE = "HARNESS.MODEL.ACCOUNTING_INCOMPLETE"
    MODEL_RESERVATION_INVALID = "HARNESS.MODEL.RESERVATION_INVALID"
    MODEL_RESERVATION_CONFLICT = "HARNESS.MODEL.RESERVATION_CONFLICT"
    MODEL_RECEIPT_MISMATCH = "HARNESS.MODEL.RECEIPT_MISMATCH"
    MODEL_GENERATION_ORPHANED = "HARNESS.MODEL.GENERATION_ORPHANED"

    TIMEOUT = "HARNESS.TIMEOUT"


class ErrorDetail(ContractModel):
    """可安全放入 ResultEnvelope 的结构化错误。"""

    code: NonEmptyString
    category: ErrorCategory
    message: NonEmptyString
    retryable: bool = False
    fallbackable: bool = False
    details: FrozenJsonMapping = Field(default_factory=dict)


class HarnessError(Exception):
    """Harness 内部异常的公共基类。"""

    default_code = ErrorCode.CAPABILITY_EXECUTION_FAILED
    default_category = ErrorCategory.CAPABILITY
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
        """转换为不包含 Python 异常对象的跨模块错误协议。"""

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


class RegistryError(HarnessError):
    default_code = ErrorCode.REGISTRY_NOT_FOUND
    default_category = ErrorCategory.REGISTRY


class PolicyError(HarnessError):
    default_code = ErrorCode.POLICY_DENIED
    default_category = ErrorCategory.POLICY


class PluginError(HarnessError):
    default_code = ErrorCode.PLUGIN_LOAD_FAILED
    default_category = ErrorCategory.PLUGIN


class CapabilityError(HarnessError):
    default_code = ErrorCode.CAPABILITY_EXECUTION_FAILED
    default_category = ErrorCategory.CAPABILITY


class ProviderError(HarnessError):
    default_code = ErrorCode.PROVIDER_EXECUTION_FAILED
    default_category = ErrorCategory.PROVIDER


class SelectionError(HarnessError):
    default_code = ErrorCode.SELECTION_INVALID_DECISION
    default_category = ErrorCategory.SELECTION


class RoutingError(HarnessError):
    default_code = ErrorCode.ROUTE_INVALID_DECISION
    default_category = ErrorCategory.ROUTE


class ContextError(HarnessError):
    default_code = ErrorCode.CONTEXT_INVALID
    default_category = ErrorCategory.CONTEXT


class PlanningError(HarnessError):
    default_code = ErrorCode.PLANNER_INVALID_OUTPUT
    default_category = ErrorCategory.PLANNER


class PlannerNotApplicableError(PlanningError):
    default_code = ErrorCode.PLANNER_NOT_APPLICABLE


class HarnessTimeoutError(HarnessError):
    default_code = ErrorCode.TIMEOUT
    default_category = ErrorCategory.TIMEOUT
    default_retryable = True
