"""InvocationContext 的阶段一构造接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from harness_contracts import InvocationContext, Request


type Clock = Callable[[], datetime]


class InvocationContextFactory(ABC):
    """把外部 Request 转换为 Runtime 内部可信的只读执行上下文。"""

    @abstractmethod
    def create(self, request: Request) -> InvocationContext:
        """为一次 Invocation 创建上下文。"""


class DefaultInvocationContextFactory(InvocationContextFactory):
    """只构造阶段一最小上下文的默认实现。

    默认实现不会把 ``request.user_id`` 或 ``request.tenant_id`` 直接提升为可信
    ``IdentityContext`` / ``TenantContext``。生产环境应在 bootstrap 层提供自己的
    ``InvocationContextFactory``，把认证、租户解析等可信结果注入 Context。
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self, request: Request) -> InvocationContext:
        if not isinstance(request, Request):
            raise TypeError("request must be Request")

        deadline_at = None
        if request.options.timeout_ms is not None:
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("context clock must return timezone-aware datetime")
            deadline_at = now + timedelta(milliseconds=request.options.timeout_ms)

        return InvocationContext(request=request, deadline_at=deadline_at)
