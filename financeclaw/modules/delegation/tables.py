"""``delegations`` 表的 SQLAlchemy ORM 映射，永久保存委派生命周期全量数据。

委派记录是委派子系统的事实来源：受理、child 绑定、状态推进与结果交付的
每个阶段都更新本表，支撑审计、幂等受理与按父运行或 child 运行的追溯查询。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class DelegationRow(Base):
    """委派记录的持久化行，对应 ``delegations`` 表，一次委派一行。

    使用场景：仓库在受理时插入本行，并在启动、状态同步、交付各阶段更新；
    外键关联 ``conversations`` 与 ``conversation_turns``，保证父子轮次可追溯，
    child run 的全表唯一约束确保一个 child run 至多归属一条委派。

    Attributes:
        delegation_id: 委派唯一标识（即 handoff_id），主键，最长 128 字符。
        tenant_id: 租户 ID，最长 128 字符，非空。
        subject_id: 主体（用户）ID，最长 128 字符，非空。
        conversation_id: 所属会话 ID，外键指向 conversations.conversation_id。
        parent_turn_id: 发起委派的父轮次 ID，外键指向 conversation_turns.turn_id。
        parent_run_id: 发起委派的父 Agent 运行 ID，最长 128 字符。
        kind: 委派种类字符串（workflow 或 agent），最长 32 字符。
        target_id: 目标标识（workflow_id 或 agent_id），最长 128 字符。
        target_version: 目标版本号（如 ``1.0.0``），最长 32 字符。
        arguments: 委派参数字典，以 JSON 存储。
        arguments_hash: 参数的 SHA-256 摘要，64 位十六进制字符串。
        request_fingerprint: 请求指纹（SHA-256），防止 handoff ID 被复用。
        authorization_decision: 授权决策，当前恒为 ``allowed``。
        policy_version: 受理时使用的委派策略版本。
        child_run_id: child 运行 ID，未启动时为 None；全表唯一。
        child_thread_id: child 的 LangGraph thread ID，未启动时为 None。
        child_server_run_id: agent server 侧运行 ID，仅 Agent 委派有值。
        status: 当前状态字符串，取值见 DelegationStatus。
        output_payload: child 成功输出的 JSON 载荷，未完成时为 None。
        error: 失败或被拒原因文本，未出错时为 None。
        created_at: 创建时间（带时区 UTC），默认 utcnow。
        updated_at: 最近更新时间（带时区 UTC），默认并在更新时刷新为 utcnow。
        completed_at: 首次进入完成、拒绝或失败终态的时间，未终态为 None。
        delivered_at: 首次交付父 Agent 的时间，未交付为 None。

    """

    __tablename__ = "delegations"
    # 唯一约束保证 child run 全表唯一；两个索引分别支撑按父运行查委派、
    # 按状态扫描未交付记录（对账）两类查询。
    __table_args__ = (
        UniqueConstraint("child_run_id", name="uq_delegations_child_run"),
        Index(
            "ix_delegations_parent",
            "tenant_id",
            "subject_id",
            "parent_run_id",
            "created_at",
        ),
        Index("ix_delegations_incomplete", "status", "updated_at"),
    )

    delegation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    parent_turn_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_turns.turn_id"), nullable=False
    )
    parent_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(String(128))
    child_thread_id: Mapped[str | None] = mapped_column(String(128))
    child_server_run_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
