"""Memory Provider / Gateway 的稳定错误类型。"""

from harness_contracts import ErrorCode, MemoryAccessError


class MemoryProviderError(MemoryAccessError):
    default_code = ErrorCode.MEMORY_PROVIDER_FAILED


class MemoryProposalConflictError(MemoryAccessError):
    default_code = ErrorCode.MEMORY_PROPOSAL_CONFLICT
