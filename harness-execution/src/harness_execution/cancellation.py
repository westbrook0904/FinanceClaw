"""进程内 Plan 取消信号及其可序列化快照。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from harness_contracts import CancellationContext


class CancellationSignal:
    """协调同一进程内的主动取消，并向 Capability 传播只读状态快照。

    ``asyncio.Event`` 只承担进程内唤醒职责，不能进入 Checkpoint；需要跨模块或
    持久化传播时使用 :meth:`snapshot` 生成 ``CancellationContext``。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None
        self._requested_at: datetime | None = None
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def cancelled(self) -> bool:
        """取消是否已经被请求。"""

        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def requested_at(self) -> datetime | None:
        return self._requested_at

    def request(self, reason: str | None = None) -> bool:
        """原子地请求一次取消；首次请求返回 True，重复请求返回 False。"""

        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise TypeError("reason must be a non-empty string when provided")
        if self._event.is_set():
            return False
        requested_at = self._clock()
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        self._reason = reason
        self._requested_at = requested_at
        self._event.set()
        return True

    async def wait(self) -> None:
        """等待取消请求；已取消时立即返回。"""

        await self._event.wait()

    def snapshot(self) -> CancellationContext:
        """返回可安全放入 ``InvocationContext`` 的不可变取消快照。"""

        return CancellationContext(
            cancelled=self.cancelled,
            reason=self._reason,
            requested_at=self._requested_at,
        )
