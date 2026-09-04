"""会话日志模块的 SQLAlchemy ORM 表定义（业务数据库持久化层）。

定义 conversations、channel_conversation_bindings、conversation_turns、
conversation_messages、conversation_summaries、model_context_manifests 与 artifacts 表。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from financeclaw.infrastructure.orm import Base, utcnow


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
        back_populates="conversation", cascade="all, delete-orphan"
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


class ConversationTurnRow(Base):
    """轮次表：持久化 ConversationTurn 记录，承载幂等键与 Agent Server 绑定。

    使用场景：BFF 幂等创建 turn 后写入本表；（租户，主体，幂等键）唯一约束
    保证 append-only；run_id 唯一约束供 Agent Server 定位轮次。

    Attributes:
        turn_id: turn 标识，主键（String(128)）。
        conversation_id: 所属会话标识，外键指向 conversations.conversation_id，非空。
        tenant_id: 租户标识（String(128)），非空，参与幂等唯一约束。
        subject_id: 主体标识（String(128)），非空，参与幂等唯一约束。
        run_id: 平台运行标识（String(128)），非空，全局唯一约束。
        server_run_id: Agent Server 运行标识（String(128)）；未绑定时为 NULL。
        client_idempotency_key: 客户端幂等键（String(200)），非空，参与幂等唯一约束。
        request_hash: 请求内容哈希（String(64)），非空。
        target_type: 目标对象类型（String(32)），非空。
        target_id: 目标对象标识（String(128)），非空。
        target_version: 目标对象版本（String(32)），非空。
        status: turn 状态字符串，非空，默认 "accepted"。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。
        completed_at: 终态完成时间（带时区）；未完成时为 NULL。
        conversation: 所属会话的关联对象（多对一）。
        messages: 该 turn 下的消息列表；随 turn 级联删除。

    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_conversation_turns_run"),
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "client_idempotency_key",
            name="uq_conversation_turns_owner_idempotency",
        ),
        Index("ix_conversation_turns_conversation_created", "conversation_id", "created_at"),
    )

    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    client_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[ConversationRow] = relationship(back_populates="turns")
    messages: Mapped[list["ConversationMessageRow"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
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
        Index("ix_messages_turn_role", "turn_id", "role"),
    )

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False)
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

    turn: Mapped[ConversationTurnRow] = relationship(back_populates="messages")


class ConversationSummaryRow(Base):
    """摘要表：持久化 ConversationSummary，支持分段与分层摘要。

    使用场景：（会话，层级，起始，结束）组合索引支撑区间查询；
    superseded_by 自引用外键记录摘要换代链路。

    Attributes:
        summary_id: 摘要标识，主键（String(128)）。
        conversation_id: 所属会话标识，外键指向 conversations.conversation_id，非空。
        level: 摘要层级（0 为分段摘要，>=1 为分层摘要），非空。
        start_sequence: 覆盖起始序号，非空。
        end_sequence: 覆盖结束序号，非空。
        source_message_ids: 源消息 ID 列表（JSON），非空，默认空列表。
        source_summary_ids: 源摘要 ID 列表（JSON），非空，默认空列表。
        summary_content: 摘要正文（Text），非空。
        topics: 主题词列表（JSON），非空，默认空列表。
        entities: 实体（股票代码）列表（JSON），非空，默认空列表。
        decisions: 历史决策列表（JSON），非空，默认空列表。
        open_items: 未决事项列表（JSON），非空，默认空列表。
        model_profile_version: 生成摘要的摘要器版本（String(32)），非空。
        template_version: 摘要模板版本（String(64)），非空。
        content_hash: 摘要内容 SHA-256 摘要（String(64)），非空。
        status: 摘要状态字符串，非空，默认 "active"。
        superseded_by: 取代本摘要的新摘要 ID（自引用外键）；未被取代时为 NULL。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。

    """

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        Index(
            "ix_summaries_conversation_range",
            "conversation_id",
            "level",
            "start_sequence",
            "end_sequence",
        ),
    )

    summary_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_summary_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    summary_content: Mapped[str] = mapped_column(Text, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    entities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    open_items: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    model_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    superseded_by: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_summaries.summary_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ModelContextManifestRow(Base):
    """模型上下文清单表：永久保存每次模型调用的 ModelContextManifest。

    使用场景：model_call_id 唯一约束保证一次调用一条清单；
    （会话，turn，run）组合索引支撑按轮次审计与回放。

    Attributes:
        manifest_id: 清单标识，主键（String(128)）。
        model_call_id: 模型调用标识（String(128)），非空，全局唯一约束。
        conversation_id: 所属会话标识，外键指向 conversations.conversation_id，非空。
        turn_id: 所属 turn 标识，外键指向 conversation_turns.turn_id，非空。
        run_id: 平台运行标识（String(128)），非空。
        prompt_template_version: 提示词模板版本（String(64)），非空。
        agent_profile_version: Agent Profile 版本（String(32)），非空。
        model_profile_version: 模型配置版本（String(32)），非空。
        recent_message_start: 入选最近原文的最小序号；无入选时为 NULL。
        recent_message_end: 入选最近原文的最大序号；无入选时为 NULL。
        summary_ids: 入选摘要 ID 列表（JSON），非空，默认空列表。
        memory_ids: 注入记忆 ID 列表（JSON），非空，默认空列表。
        memory_refs: 记忆引用明细（JSON 对象列表），非空，默认空列表。
        historical_message_ids: 相关古老历史消息 ID 列表（JSON），非空，默认空列表。
        tool_result_refs: 外置工件 ID 列表（JSON），非空，默认空列表。
        exposed_tools: 暴露给模型的工具清单（JSON），非空，默认空列表。
        input_token_count: 估算输入 token 总数，非空。
        available_input_tokens: 配置的可用输入预算，非空。
        omissions: 省略明细（JSON 对象列表），非空，默认空列表。
        context_hash: 上下文 SHA-256 摘要（String(64)），非空。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。

    """

    __tablename__ = "model_context_manifests"
    __table_args__ = (
        UniqueConstraint("model_call_id", name="uq_manifests_model_call"),
        Index("ix_manifests_run", "conversation_id", "turn_id", "run_id"),
    )

    manifest_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    recent_message_start: Mapped[int | None] = mapped_column(Integer)
    recent_message_end: Mapped[int | None] = mapped_column(Integer)
    summary_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    memory_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    historical_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_result_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exposed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    omissions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ArtifactMetadataRow(Base):
    """工件元数据表：记录外置工具结果等工件的存储与访问信息。

    使用场景：工具结果超出保留预算时正文外置到对象存储，本表保存其存储 URI、
    内容哈希、大小与访问/加密策略，供审计与按需回读。

    Attributes:
        artifact_id: 工件标识，主键（String(128)）。
        tenant_id: 租户标识（String(128)），非空，参与归属索引。
        subject_id: 主体标识（String(128)），非空，参与归属索引。
        content_type: 工件 MIME 类型（String(200)），非空。
        storage_uri: 对象存储 URI（Text），非空。
        content_hash: 内容 SHA-256 摘要（String(64)），非空。
        size_bytes: 内容字节数，非空。
        source_type: 产生工件的来源类型（String(64)），非空。
        source_id: 来源对象标识（String(128)），非空。
        access_policy: 访问策略（JSON 对象），非空，默认空对象。
        encryption_metadata: 加密元数据（JSON 对象），非空，默认空对象。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。

    """

    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_owner", "tenant_id", "subject_id", "artifact_id"),)

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    access_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    encryption_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
