"""Harness 的业务无关公共协议。

其他模块应从本包顶层导入稳定类型，不依赖内部文件布局。
"""

from .base import ContractModel, JsonPrimitive, JsonValue, MutableContractModel
from .capability import CapabilityDescriptor, CapabilityType
from .context import (
    CancellationContext,
    ExecutionState,
    ExecutionStatus,
    IdentityContext,
    InvocationContext,
    TenantContext,
    TraceContext,
)
from .errors import (
    CapabilityError,
    ErrorCategory,
    ErrorCode,
    ErrorDetail,
    HarnessError,
    HarnessTimeoutError,
    PluginError,
    PolicyError,
    RegistryError,
    RequestError,
)
from .request import Request, RequestInput, RequestOptions, RequestTarget
from .result import ResultEnvelope, ResultOutput, ResultStatus

__all__ = [
    "CancellationContext",
    "CapabilityDescriptor",
    "CapabilityError",
    "CapabilityType",
    "ContractModel",
    "ErrorCategory",
    "ErrorCode",
    "ErrorDetail",
    "ExecutionState",
    "ExecutionStatus",
    "HarnessError",
    "HarnessTimeoutError",
    "IdentityContext",
    "InvocationContext",
    "JsonPrimitive",
    "JsonValue",
    "MutableContractModel",
    "PluginError",
    "PolicyError",
    "RegistryError",
    "Request",
    "RequestError",
    "RequestInput",
    "RequestOptions",
    "RequestTarget",
    "ResultEnvelope",
    "ResultOutput",
    "ResultStatus",
    "TenantContext",
    "TraceContext",
]
