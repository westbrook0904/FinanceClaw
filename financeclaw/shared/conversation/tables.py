"""会话日志模块的 SQLAlchemy ORM 表定义（业务数据库持久化层）。

定义 conversations、channel_conversation_bindings、conversation_turns、
conversation_messages、model_context_manifests 与 artifacts 表。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from financeclaw.shared.turns.tables import ConversationTurnRow

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from financeclaw.shared.infrastructure.orm import Base, utcnow


class ConversationRow(Base):
    """会话表：持久化 Conversation 领域记录，一个会话固定绑定一个 Agent 线程。

    使用场景：作为 Conversation Journal 的根表；agent_thread_id 全局唯一，
    支撑 LangGraph 线程映射与跨 Agent Server 重启继续。

    Attributes:
        conversation_id: 会话标识，主键（String(128)）。
        tenant_id: 租户标识（String(128)），非空，与 subject_id 组成归属索引。
        subject_id: 主体标识（String(128)），非空。
        agent_id: Agent 标识（String(128)），非空。
        agent_profile_version: 创建会话时的 Agent Profile 版本（String(32)），非空。
        agent_thread_id: Agent 线程 UUID（String(128)），非空，全局唯一约束。
        status: 会话状态字符串，非空，默认 "active"。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。
        updated_at: 更新时间（带时区），非空，默认当前 UTC 时间且随更新刷新。
        turns: 关联的 turn 列表；随会话级联删除。

    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner", "tenant_id", "subject_id", "conversation_id"),
        UniqueConstraint("agent_thread_id", name="uq_conversations_agent_thread"),
        UniqueConstraint(
            "conversation_id", "tenant_id", "subject_id", name="uq_conversation_owner"
        ),
    )

    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    turns: Mapped[list["ConversationTurnRow"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        foreign_keys="ConversationTurnRow.conversation_id",
    )


class ChannelConversationBindingRow(Base):
    """Channel 单聊绑定表：把外部 chat 唯一映射到一个 Conversation。

    Attributes:
        binding_id: 绑定主键。
        channel: Channel 类型，一期固定为 feishu。
        app_id: 飞书应用 ID。
        tenant_key: 飞书租户键。
        external_user_id: 发件人 open_id。
        external_chat_id: P2P chat_id。
        conversation_id: FinanceClaw Conversation 外键。
        created_at: 创建时间。
        updated_at: 最近解析时间。

    """

    __tablename__ = "channel_conversation_bindings"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "app_id",
            "tenant_key",
            "external_chat_id",
            name="uq_channel_conversation_bindings_chat",
        ),
        Index(
            "ix_channel_conversation_bindings_user",
            "channel",
            "app_id",
            "tenant_key",
            "external_user_id",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class ConversationMessageRow(Base):
    """消息表：持久化 ConversationMessage 原文日志，append-only 且按序号排列。

    使用场景：（会话，序号）唯一约束保证序号不重复；turn_id+role 索引支撑
    幂等对账查询；上下文装配按序号顺序读取原文。

    Attributes:
        message_id: 消息标识，主键（String(128)）。
        conversation_id: 所属会话标识，外键指向 conversations.conversation_id，非空。
        turn_id: 所属 turn 标识，外键指向 conversation_turns.turn_id，非空。
        sequence: 会话内序号，非空，与 conversation_id 组成唯一约束。
        parent_message_id: 父消息标识（自引用外键）；分支消息使用，顶层为 NULL。
        role: 消息角色字符串（user/assistant），非空。
        content: 消息原文（Text），非空。
        content_hash: 内容 SHA-256 摘要（String(64)），非空，用于对账。
        visible: 是否可见，非空，默认 True。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。
        turn: 所属 turn 的关联对象（多对一）。

    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_sequence"),
        UniqueConstraint("turn_id", "message_id", name="uq_message_turn"),
        ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["conversation_turns.turn_id", "conversation_turns.conversation_id"],
            name="fk_message_turn_conversation",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_messages_turn_role", "turn_id", "role"),
        Index(
            "uq_messages_final_assistant",
            "turn_id",
            unique=True,
            sqlite_where=text("role = 'assistant' AND parent_message_id IS NULL"),
            postgresql_where=text("role = 'assistant' AND parent_message_id IS NULL"),
        ),
    )

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id")
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    turn: Mapped["ConversationTurnRow"] = relationship(
        back_populates="messages", foreign_keys=[turn_id]
    )


class ModelContextManifestRow(Base):
    """每次实际模型调用的来源、预算和请求指纹。"""

    __tablename__ = "model_context_manifests"
    __table_args__ = (
        UniqueConstraint("model_call_id", name="uq_manifests_model_call"),
        Index("ix_manifests_turn", "conversation_id", "turn_id"),
    )

    manifest_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    subtype: Mapped[str] = mapped_column(String(32), nullable=False)
    token_count_method: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    memory_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_result_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exposed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    omissions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
