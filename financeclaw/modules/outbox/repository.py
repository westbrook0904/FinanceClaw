"""Outbox 事件的持久化仓库：Protocol 接口与 SQLAlchemy 实现。

实现租约式（lease）领取：多个 publisher 并发运行时借助 ``FOR UPDATE SKIP
LOCKED`` 行锁与 ``locked_until`` 租约互斥；投递失败按指数退避重试，超过
上限转入死信。
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import OutboxEvent, OutboxStatus
from .tables import OutboxEventRow


class OutboxRepository(Protocol):
    """Outbox 仓库接口，抽象事件领取与投递结果的写回操作。

    使用场景：OutboxPublisher 依赖该协议批量领取事件并回写成功或失败结果；
    生产环境使用 SqlAlchemyOutboxRepository，测试可替换为内存实现。
    """

    def claim_pending(self, *, limit: int, lease_seconds: int = 60) -> tuple[OutboxEvent, ...]:
        """领取一批到期可投递的事件并为其设置投递租约。

        Args:
            limit: 单次最多领取的事件数。
            lease_seconds: 租约时长（秒），超时未确认可被其他 publisher 接管。

        Returns:
            已置为 PUBLISHING 并锁定租约的事件快照元组。

        """
        ...

    def mark_published(self, event_id: str) -> None:
        """把租约内的事件标记为 PUBLISHED 并记录投递成功时间。

        Args:
            event_id: 事件唯一标识。

        Raises:
            LookupError: 事件不存在或未处于本 publisher 的 PUBLISHING 租约中。

        """
        ...

    def mark_failed(self, event_id: str, error: str, *, max_attempts: int) -> None:
        """记录一次投递失败：未达上限则指数退避重试，否则转入死信。

        Args:
            event_id: 事件唯一标识。
            error: 失败原因描述（存储时截断到 1000 字符）。
            max_attempts: 允许的最大尝试次数，达到后事件进入 DEAD_LETTER。

        Raises:
            LookupError: 事件不存在或未处于本 publisher 的 PUBLISHING 租约中。

        """
        ...


class SqlAlchemyOutboxRepository:
    """基于 SQLAlchemy 的 Outbox 仓库实现，写操作均运行在独立事务中。

    使用场景：生产环境注入 sessionmaker 后供 OutboxPublisher 使用；领取操作
    通过行级锁与租约保证多实例并发下的互斥与故障接管。
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """初始化仓库。

        Args:
            sessions: 指向业务库的 SQLAlchemy sessionmaker 工厂。

        """
        self._sessions = sessions

    def claim_pending(self, *, limit: int, lease_seconds: int = 60) -> tuple[OutboxEvent, ...]:
        """领取一批到期事件，置为 PUBLISHING 并写入租约到期时间。

        Args:
            limit: 单次最多领取的事件数。
            lease_seconds: 租约时长（秒），用于失联 publisher 的租约回收。

        Returns:
            被本次领取锁定的 OutboxEvent 快照元组（可能为空）。

        """
        now = datetime.now(UTC)
        # 1. 组装领取查询：待投递，或投递中但租约已过期（前次发布者失联）的事件。
        eligible = or_(
            OutboxEventRow.status == OutboxStatus.PENDING.value,
            (
                (OutboxEventRow.status == OutboxStatus.PUBLISHING.value)
                & (OutboxEventRow.locked_until < now)
            ),
        )
        statement = (
            select(OutboxEventRow)
            .where(eligible, OutboxEventRow.available_at <= now)
            .order_by(OutboxEventRow.available_at, OutboxEventRow.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        with self._sessions.begin() as session:
            # 2. FOR UPDATE SKIP LOCKED 保证多 publisher 并发领取互不重复、互不阻塞。
            rows = tuple(session.scalars(statement))
            # 3. 将领到的事件置为 PUBLISHING 并写入租约到期时间，随事务一起提交。
            for row in rows:
                row.status = OutboxStatus.PUBLISHING.value
                row.locked_until = now + timedelta(seconds=lease_seconds)
            return tuple(_event(row) for row in rows)

    def mark_published(self, event_id: str) -> None:
        """把租约内的事件标记为 PUBLISHED，清空租约与错误信息。"""
        with self._sessions.begin() as session:
            # 1. 校验事件仍处于本 publisher 的 PUBLISHING 租约中。
            row = _owned_claim(session, event_id)
            # 2. 写入成功状态与投递时间，并释放租约。
            row.status = OutboxStatus.PUBLISHED.value
            row.published_at = datetime.now(UTC)
            row.locked_until = None
            row.last_error = None

    def mark_failed(self, event_id: str, error: str, *, max_attempts: int) -> None:
        """记录投递失败：指数退避后重试，达到上限转入死信。"""
        with self._sessions.begin() as session:
            # 1. 校验租约归属，累计尝试次数并记录失败原因（截断到 1000 字符）。
            row = _owned_claim(session, event_id)
            row.attempts += 1
            row.last_error = error[:1_000]
            row.locked_until = None
            # 2. 达到最大尝试次数则进入死信，等待人工介入。
            if row.attempts >= max_attempts:
                row.status = OutboxStatus.DEAD_LETTER.value
            else:
                # 3. 否则回退为 PENDING，并按指数退避（上限 300 秒）推迟可领取时间。
                row.status = OutboxStatus.PENDING.value
                delay_seconds = min(300, 2 ** min(row.attempts, 8))
                row.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)


def _owned_claim(session: Session, event_id: str) -> OutboxEventRow:
    """取回事件行并校验其仍处于 PUBLISHING 租约中，否则视为租约已失效。"""
    row = session.get(OutboxEventRow, event_id)
    if row is None or row.status != OutboxStatus.PUBLISHING.value:
        raise LookupError("outbox event is not owned by this publisher lease")
    return row


def _event(row: OutboxEventRow) -> OutboxEvent:
    """把 ORM 行转换为不可变的 OutboxEvent 领域模型。"""
    return OutboxEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        payload=row.payload,
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        available_at=row.available_at,
        locked_until=row.locked_until,
        created_at=row.created_at,
        published_at=row.published_at,
        last_error=row.last_error,
    )
