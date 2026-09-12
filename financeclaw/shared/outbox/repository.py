"""Outbox 事件的持久化仓库：Protocol 接口与 SQLAlchemy 实现。

实现租约式（lease）领取：多个 publisher 并发运行时借助 ``FOR UPDATE SKIP
LOCKED`` 行锁与 ``locked_until`` 租约互斥；投递失败按指数退避重试，超过
上限转入死信。
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.shared.outbox.models import OutboxEvent, OutboxStatus
from financeclaw.shared.outbox.tables import OutboxEventRow


class OutboxRepository(Protocol):
    """Outbox 仓库接口，抽象事件领取与投递结果的写回操作。

    使用场景：OutboxPublisher 依赖该协议批量领取事件并回写成功或失败结果；
    生产环境使用 SqlAlchemyOutboxRepository，测试可替换为内存实现。
    """

    def enqueue(self, event: OutboxEvent) -> None:
        """先持久化可重试任务；相同事件 ID 必须标识相同任务。"""
        ...

    def claim_pending(
        self, *, limit: int, lease_seconds: int = 60, destination: str = "audit"
    ) -> tuple[OutboxEvent, ...]:
        """领取一批到期可投递的事件并为其设置投递租约。

        Args:
            destination: 当前消费者负责的定向事件。
            limit: 单次最多领取的事件数。
            lease_seconds: 租约时长（秒），超时未确认可被其他 publisher 接管。

        Returns:
            已置为 PUBLISHING 并锁定租约的事件快照元组。

        """
        ...

    def mark_published(self, event_id: str, *, claim_epoch: int) -> None:
        """把租约内的事件标记为 PUBLISHED 并记录投递成功时间。

        Args:
            event_id: 事件唯一标识。
            claim_epoch: 领取时返回的租约代际。

        Raises:
            LookupError: 事件不存在或未处于本 publisher 的 PUBLISHING 租约中。

        """
        ...

    def mark_failed(
        self, event_id: str, error: str, *, max_attempts: int, claim_epoch: int
    ) -> None:
        """记录一次投递失败：未达上限则指数退避重试，否则转入死信。

        Args:
            event_id: 事件唯一标识。
            claim_epoch: 领取时返回的租约代际。
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

    def enqueue(self, event: OutboxEvent) -> None:
        """幂等插入任务，投递字段由仓库管理。"""
        with self._sessions.begin() as session:
            self.enqueue_in_session(session, event)

    def enqueue_in_session(self, session: Session, event: OutboxEvent) -> None:
        """与业务事实共用事务插入不可变任务，不以重投覆盖处理预算。"""
        existing = session.get(OutboxEventRow, event.event_id)
        if existing is not None:
            facts = ("destination", "event_type", "tenant_id", "subject_id", "payload")
            if any(getattr(existing, key) != getattr(event, key) for key in facts):
                raise ValueError("outbox event identifies different task facts")
            return
        session.add(OutboxEventRow(**event.model_dump()))

    def get(self, event_id: str) -> OutboxEvent:
        """读取持久任务快照，包含跨进程重启仍保留的预算。"""
        with self._sessions() as session:
            row = session.get(OutboxEventRow, event_id)
            if row is None:
                raise LookupError("outbox event not found")
            return _event(row)

    def require_claim_in_session(
        self, session: Session, event_id: str, claim_epoch: int
    ) -> OutboxEventRow:
        """在调用方业务事务末尾验证并锁住租约，防止旧消费者提交。"""
        return _owned_claim(session, event_id, claim_epoch)

    def update_metadata_in_session(
        self, session: Session, event_id: str, claim_epoch: int, metadata: dict[str, Any]
    ) -> OutboxEventRow:
        """更新服务端控制元数据；完整字典赋值保证 JSON 更新被 ORM 跟踪。"""
        row = self.require_claim_in_session(session, event_id, claim_epoch)
        row.processing_metadata = {**(row.processing_metadata or {}), **deepcopy(metadata)}
        return row

    def complete_in_session(
        self,
        session: Session,
        event_id: str,
        claim_epoch: int,
        *,
        outcome: str = "completed",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """产物与完成标记必须同事务提交；任何租约冲突回滚全部业务写入。"""
        row = self.update_metadata_in_session(
            session, event_id, claim_epoch, {**(metadata or {}), "outcome": outcome}
        )
        row.status = OutboxStatus.PUBLISHED.value
        row.published_at = datetime.now(UTC)
        row.locked_until = None
        row.last_error = None

    def renew_claim(self, event_id: str, *, claim_epoch: int, lease_seconds: int) -> None:
        """仅锁 Outbox 的短事务续租；过期租约不能自行复活。"""
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            row = self.require_claim_in_session(session, event_id, claim_epoch)
            row.locked_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)

    def reserve_model_attempt(
        self,
        event_id: str,
        *,
        claim_epoch: int,
        max_attempts: int,
        input_tokens: int,
        output_tokens: int,
        snapshot_id: str = "default",
        max_snapshot_attempts: int = 2,
        max_snapshots: int = 3,
    ) -> dict[str, Any]:
        """模型请求前永久预留次数与 token；响应未知或进程崩溃也不退还。"""
        if (
            min(input_tokens, output_tokens) < 0
            or min(max_attempts, max_snapshot_attempts, max_snapshots) < 1
        ):
            raise ValueError("invalid model budget")
        with self._sessions.begin() as session:
            row = self.require_claim_in_session(session, event_id, claim_epoch)
            metadata = deepcopy(row.processing_metadata or {})
            budget = metadata.setdefault("model_budget", {})
            snapshots = budget.setdefault("snapshots", {})
            attempts = budget.get("attempts", 0)
            count = snapshots.get(snapshot_id, 0)
            if (
                attempts >= max_attempts
                or count >= max_snapshot_attempts
                or (snapshot_id not in snapshots and len(snapshots) >= max_snapshots)
            ):
                raise ModelBudgetExhausted("durable model attempt budget exhausted")
            snapshots[snapshot_id] = count + 1
            budget["attempts"] = attempts + 1
            budget["reserved_input_tokens"] = budget.get("reserved_input_tokens", 0) + input_tokens
            budget["reserved_output_tokens"] = (
                budget.get("reserved_output_tokens", 0) + output_tokens
            )
            row.processing_metadata = metadata
            return deepcopy(budget)

    def record_model_usage(self, event_id: str, *, claim_epoch: int, usage: dict[str, Any]) -> None:
        """记录有响应调用的实际用量；未知调用仍保留先前预留上界。"""
        with self._sessions.begin() as session:
            row = self.require_claim_in_session(session, event_id, claim_epoch)
            metadata = deepcopy(row.processing_metadata or {})
            samples = list(metadata.get("model_usage", []))
            samples.append(
                {
                    key: int(usage[key])
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                    if isinstance(usage.get(key), int) and usage[key] >= 0
                }
            )
            metadata["model_usage"] = samples[-8:]
            row.processing_metadata = metadata

    def claim_pending(
        self, *, limit: int, lease_seconds: int = 60, destination: str = "audit"
    ) -> tuple[OutboxEvent, ...]:
        """领取一批到期事件，置为 PUBLISHING 并写入租约到期时间。

        Args:
            destination: 当前消费者负责的定向事件。
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
            .where(
                eligible,
                OutboxEventRow.available_at <= now,
                OutboxEventRow.destination == destination,
            )
            .order_by(OutboxEventRow.available_at, OutboxEventRow.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        with self._sessions.begin() as session:
            # 2. FOR UPDATE SKIP LOCKED 保证多 publisher 并发领取互不重复、互不阻塞。
            rows = tuple(session.scalars(statement))
            # 3. 将领到的事件置为 PUBLISHING 并写入租约到期时间，随事务一起提交。
            for row in rows:
                if row.status == OutboxStatus.PUBLISHING.value:
                    metadata = deepcopy(row.processing_metadata or {})
                    metadata["lease_takeovers"] = metadata.get("lease_takeovers", 0) + 1
                    row.processing_metadata = metadata
                row.claim_epoch += 1
                row.status = OutboxStatus.PUBLISHING.value
                row.locked_until = now + timedelta(seconds=lease_seconds)
            return tuple(_event(row) for row in rows)

    def mark_published(self, event_id: str, *, claim_epoch: int) -> None:
        """把租约内的事件标记为 PUBLISHED，清空租约与错误信息。"""
        with self._sessions.begin() as session:
            self.complete_in_session(session, event_id, claim_epoch)

    def mark_failed(
        self, event_id: str, error: str, *, max_attempts: int, claim_epoch: int
    ) -> None:
        """记录投递失败：指数退避后重试，达到上限转入死信。"""
        with self._sessions.begin() as session:
            self.fail_in_session(
                session, event_id, claim_epoch, error=error, max_attempts=max_attempts
            )

    def fail_in_session(
        self,
        session: Session,
        event_id: str,
        claim_epoch: int,
        *,
        error: str,
        max_attempts: int,
        terminal: bool = False,
    ) -> bool:
        """同事务分类失败；返回是否死信，供 owner 指针和输入隔离共同提交。"""
        row = self.require_claim_in_session(session, event_id, claim_epoch)
        metadata = deepcopy(row.processing_metadata or {})
        metadata["failure_count"] = metadata.get("failure_count", 0) + 1
        metadata["last_failure_class"] = error[:128]
        row.processing_metadata = metadata
        row.attempts += 1
        row.last_error = error[:1_000]
        row.locked_until = None
        dead = terminal or row.attempts >= max_attempts
        row.status = OutboxStatus.DEAD_LETTER.value if dead else OutboxStatus.PENDING.value
        if not dead:
            row.available_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2 ** min(row.attempts, 8))
            )
        return dead

    def record_version_conflict(self, event_id: str, *, claim_epoch: int) -> None:
        """Persist bounded conflict statistics while retaining the current event's budget."""
        with self._sessions.begin() as session:
            row = self.require_claim_in_session(session, event_id, claim_epoch)
            metadata = deepcopy(row.processing_metadata or {})
            metadata["version_conflicts"] = metadata.get("version_conflicts", 0) + 1
            row.processing_metadata = metadata


class ModelBudgetExhausted(RuntimeError):
    """有限模型额度已耗尽；基础设施重试不能重置该额度。"""


def _owned_claim(session: Session, event_id: str, claim_epoch: int) -> OutboxEventRow:
    """取回事件行并校验其仍处于 PUBLISHING 租约中，否则视为租约已失效。"""
    row = session.scalar(
        select(OutboxEventRow).where(OutboxEventRow.event_id == event_id).with_for_update()
    )
    if (
        row is None
        or row.status != OutboxStatus.PUBLISHING.value
        or row.claim_epoch != claim_epoch
        or row.locked_until is None
        or row.locked_until.replace(tzinfo=row.locked_until.tzinfo or UTC) <= datetime.now(UTC)
    ):
        raise LookupError("outbox event is not owned by this publisher lease")
    return row


def _event(row: OutboxEventRow) -> OutboxEvent:
    """把 ORM 行转换为不可变的 OutboxEvent 领域模型。"""
    return OutboxEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        destination=row.destination,
        claim_epoch=row.claim_epoch,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        payload=row.payload,
        processing_metadata=row.processing_metadata or {},
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        available_at=row.available_at,
        locked_until=row.locked_until,
        created_at=row.created_at,
        published_at=row.published_at,
        last_error=row.last_error,
    )
