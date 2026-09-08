"""Outbox（事务性发件箱）领域的 Pydantic 模型：事件结构与投递状态机。

本模块定义与永久 Audit 在同一数据库事务落盘的 Outbox 事件快照；异步
publisher 依据其中的租约字段（attempts、available_at、locked_until）实现
多实例间安全的可靠投递。
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OutboxStatus(StrEnum):
    """Outbox 事件的投递状态，覆盖从待投递到死信的完整生命周期。

    使用场景：仓库在领取、发布成功、发布失败时更新事件状态，publisher 依据
    状态与租约到期时间判断事件是否可再次被领取。

    Attributes:
        PENDING: 待投递，可被 publisher 领取。
        PUBLISHING: 已被某个 publisher 持租约投递中，租约到期前不可再领取。
        PUBLISHED: 投递成功，事件处理完毕。
        DEAD_LETTER: 重试次数达到上限仍失败，进入死信等待人工处理。

    """

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class OutboxEvent(BaseModel):
    """一条待投递的 Outbox 事件快照，包含投递进度与租约信息。

    使用场景：与 Audit 记录在同一数据库事务中创建（Transactional Outbox
    模式），由异步 publisher 领取后投递到下游 sink；模型不可变，投递进度
    变化由仓库写回 ORM 行而非修改本对象。

    Attributes:
        event_id: 事件唯一标识，作 ``outbox_events`` 表主键；审计事件固定
            形如 ``outbox-<audit_id>``。
        event_type: 事件类型字符串（与 Audit 事件类型对齐）。
        aggregate_type: 事件所属聚合的类型（如 resource_type）。
        aggregate_id: 事件所属聚合的标识（如 resource_id）。
        tenant_id: 租户 ID，用于多租户隔离与按归属查询。
        subject_id: 主体（用户）ID。
        payload: 投递给下游的事件载荷字典，默认为空字典。
        status: 当前投递状态，默认 PENDING。
        attempts: 已尝试投递次数，从 0 起计。
        available_at: 事件何时可被领取；失败重试时按指数退避向后推移。
        locked_until: 当前租约到期时间；非 None 表示处于 PUBLISHING 租约中。
        created_at: 事件创建时间（UTC），默认当前时间。
        published_at: 投递成功时间，未投递为 None。
        last_error: 最近一次投递失败的原因（存储时截断），无失败为 None。

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
