"""FinanceClaw 事务性发件箱（Transactional Outbox）模块的公开接口。

每条永久 Audit 与有界 Outbox 事件在同一数据库事务落盘；本包对外暴露事件
模型、投递状态枚举、事件仓库与异步投递器，供应用层与后台任务组装可靠外
发链路。
"""

from .models import OutboxEvent, OutboxStatus
from .publisher import OutboxPublisher, OutboxSink
from .repository import OutboxRepository, SqlAlchemyOutboxRepository

__all__ = [
    "OutboxEvent",
    "OutboxPublisher",
    "OutboxRepository",
    "OutboxSink",
    "OutboxStatus",
    "SqlAlchemyOutboxRepository",
]
