"""批量发布待处理 Outbox 事件并记录成功或失败结果。"""

from typing import Protocol

from .models import OutboxEvent
from .repository import OutboxRepository


class OutboxSink(Protocol):
    """定义OutboxSink。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    async def publish(self, event: OutboxEvent) -> None:
        """调用事件发布函数；发布成功后由调用方负责更新持久化状态。"""
        ...


class OutboxPublisher:
    """领取待投递事件，调用发布函数，并把投递结果可靠回写。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        _repository: 负责领域状态读写和事务一致性的仓储。
        _sink: 接收已领取 Outbox 事件的发布函数。
        _batch_size: 内部 `batch size` 状态或依赖，不属于公开接口。
        _max_attempts: 限制该资源或操作的最大允许值。
    """

    def __init__(
        self,
        repository: OutboxRepository,
        sink: OutboxSink,
        *,
        batch_size: int = 100,
        max_attempts: int = 8,
    ) -> None:
        """注入并保存OutboxPublisher所需的协作对象，同时校验构造期不变量。"""
        self._repository = repository
        self._sink = sink
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        """领取并尝试发布一批到期 Outbox 事件，返回成功发布数量。"""
        events = self._repository.claim_pending(limit=self._batch_size)
        for event in events:
            try:
                await self._sink.publish(event)
            except Exception as exc:
                self._repository.mark_failed(
                    event.event_id,
                    f"{type(exc).__name__}: {exc}",
                    max_attempts=self._max_attempts,
                )
            else:
                self._repository.mark_published(event.event_id)
        return len(events)
