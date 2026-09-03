"""定义事务 Outbox 事件及其投递状态。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OutboxStatus(StrEnum):
    """Outbox 事件等待、完成或永久失败的投递状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        PENDING: 操作已创建但尚未开始处理。
        PUBLISHING: 表示 `publishing` 这一受限枚举值。
        PUBLISHED: 表示 `published` 这一受限枚举值。
        DEAD_LETTER: 表示 `dead_letter` 这一受限枚举值。
    """

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class OutboxEvent(BaseModel):
    """定义与业务事务一起写入、等待可靠投递的领域事件。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        event_id: 审计或 Outbox 事件的稳定标识。
        event_type: 事件的语义类型，供消费者选择处理逻辑。
        aggregate_type: 产生 Outbox 事件的聚合根类别。
        aggregate_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        payload: 事件携带的结构化业务数据。
        status: 当前生命周期状态，决定记录允许的后续操作。
        attempts: 已经尝试投递或执行的次数。
        available_at: 该生命周期事件发生的 UTC 时间。
        locked_until: 事件领取租约的到期时间，防止多个发布者重复处理。
        created_at: 记录创建时间，统一按 UTC 解释。
        published_at: 该生命周期事件发生的 UTC 时间。
        last_error: 最近一次失败原因，尚未失败时为空。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    tenant_id: str
    subject_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    locked_until: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None
    last_error: str | None = None
