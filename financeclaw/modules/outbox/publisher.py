"""Outbox 异步投递器：从仓库批量领取事件并投递到可插拔的下游 sink。

实现 Transactional Outbox 模式的消费端：单轮批量领取、逐条投递，失败记录
原因并交给仓库做退避或死信处理，成功则标记完成；自身不管理调度周期。
"""

from typing import Protocol

from .models import OutboxEvent
from .repository import OutboxRepository


class OutboxSink(Protocol):
    """Outbox 事件的投递目标协议，由具体下游（如消息队列、webhook）实现。

    使用场景：OutboxPublisher 持有一个 sink 实例，把领取到的每条事件异步
    投递出去；实现方只需提供 ``publish`` 协程，失败时抛出任意异常即可。
    """

    async def publish(self, event: OutboxEvent) -> None:
        """投递单条 Outbox 事件到下游。

        Args:
            event: 待投递的事件快照。

        Raises:
            Exception: 投递失败时抛出的任意异常，由 publisher 记为一次失败。

        """
        ...


class OutboxPublisher:
    """Outbox 事件投递器：批量领取、逐条投递并回写结果。

    使用场景：由后台任务或应用启动逻辑周期性调用 ``run_once``，把与 Audit
    同事务落盘的 Outbox 事件可靠投递到下游 sink；批次大小与最大尝试次数
    在构造时可调。
    """

    def __init__(
        self,
        repository: OutboxRepository,
        sink: OutboxSink,
        *,
        batch_size: int = 100,
        max_attempts: int = 8,
    ) -> None:
        """初始化投递器。

        Args:
            repository: Outbox 事件仓库，负责领取与投递结果回写。
            sink: 投递目标，接收单条事件并执行实际外发。
            batch_size: 单轮 ``run_once`` 最多处理的事件数，默认 100。
            max_attempts: 单个事件允许的最大尝试次数，超过转入死信，默认 8。

        """
        self._repository = repository
        self._sink = sink
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def run_once(self) -> int:
        """执行一轮投递：领取一批事件并逐条投递，返回本轮处理的事件数。

        Returns:
            本轮从仓库领到并处理（无论成功或失败）的事件数量。

        """
        # 1. 按批次大小领取处于租约保护下的待投递事件。
        events = self._repository.claim_pending(limit=self._batch_size)
        for event in events:
            try:
                # 2. 投递到下游 sink。
                await self._sink.publish(event)
            except Exception as exc:
                # 3. 失败：记录原因，由仓库决定退避重试或转入死信。
                self._repository.mark_failed(
                    event.event_id,
                    f"{type(exc).__name__}: {exc}",
                    max_attempts=self._max_attempts,
                )
            else:
                # 4. 成功：把事件标记为已发布。
                self._repository.mark_published(event.event_id)
        return len(events)
