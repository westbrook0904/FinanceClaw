"""声明委派记录的 SQLAlchemy 持久化映射。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class DelegationRow(Base):
    """定义委派Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        delegation_id: 一次父子运行委派的稳定标识。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        parent_turn_id: 发起委派的父会话轮次标识。
        parent_run_id: 发起委派的父运行标识。
        kind: 记录或目标的语义类别。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
        arguments: 传给目标工具或工作流的已解析参数。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        request_fingerprint: 委派或工作流请求的稳定指纹，用于幂等冲突检测。
        authorization_decision: 执行前记录的策略授权结果。
        policy_version: 作出决策时使用的策略版本。
        child_run_id: 实际执行委派任务的子运行标识。
        child_thread_id: 子 Agent 保存检查点与消息的线程标识。
        child_server_run_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        status: 当前生命周期状态，决定记录允许的后续操作。
        output_payload: 运行终态时保存的结构化输出快照。
        error: 失败原因的稳定文本；成功或未结束时为空。
        created_at: 记录创建时间，统一按 UTC 解释。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        completed_at: 进入成功或失败终态的时间；未结束时为空。
        delivered_at: 该生命周期事件发生的 UTC 时间。
    """

    __tablename__ = "delegations"
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
