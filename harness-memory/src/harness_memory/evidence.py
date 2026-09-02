"""Memory proposal evidence 的可信解析边界。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import InvocationContext


class MemoryEvidenceResolver(ABC):
    @abstractmethod
    def resolves(
        self,
        invocation: InvocationContext,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        """所有 evidence ref 都能解析到本次可信事实时返回 True。"""


class RequestEvidenceResolver(MemoryEvidenceResolver):
    """默认只承认当前 Request；Stage 3 可组合持久化 Result resolver。"""

    def resolves(
        self,
        invocation: InvocationContext,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        expected = f"request:{invocation.request.request_id}"
        return bool(evidence_refs) and all(reference == expected for reference in evidence_refs)
