"""声明会话 Journal 相关 SQLAlchemy 表映射。"""

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
    """定义会话Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        agent_id: Agent 配置的稳定标识。
        agent_profile_version: 本次运行固定使用的 Agent 配置版本。
        agent_thread_id: 根 Agent 使用的服务端线程标识。
        status: 当前生命周期状态，决定记录允许的后续操作。
        created_at: 记录创建时间，统一按 UTC 解释。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        turns: 该会话通过 ORM 关系加载的轮次集合。
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


class ConversationTurnRow(Base):
    """定义会话轮次Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        server_run_id: Agent Server 侧运行标识；尚未提交远端运行时为空。
        client_idempotency_key: 客户端幂等键，在同一资源范围内唯一。
        request_hash: 请求规范化后的哈希，用于检测幂等键复用冲突。
        target_type: 目标类别，用于区分 Agent、工具和工作流。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
        status: 当前生命周期状态，决定记录允许的后续操作。
        created_at: 记录创建时间，统一按 UTC 解释。
        completed_at: 进入成功或失败终态的时间；未结束时为空。
        conversation: 该记录所属的会话 ORM 对象。
        messages: 按会话顺序排列的消息集合。
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
    """定义会话消息Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        message_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        sequence: 消息在会话内从 1 开始的稳定顺序号。
        parent_message_id: 被该消息直接响应的父消息标识；无父消息时为空。
        role: 消息发送方角色。
        content: 经过边界校验后保存或传递的正文内容。
        content_hash: 正文的 SHA-256，用于完整性校验、去重与审计。
        visible: 该消息是否应暴露给后续模型上下文。
        created_at: 记录创建时间，统一按 UTC 解释。
        turn: 该消息所属的会话轮次 ORM 对象。
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
    """定义会话摘要Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        summary_id: 摘要稳定标识。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        level: 摘要层级；0 表示直接由原始消息生成。
        start_sequence: 摘要覆盖的第一条消息序号。
        end_sequence: 摘要覆盖的最后一条消息序号。
        source_message_ids: 生成摘要时使用的原始消息标识，保留证据链。
        source_summary_ids: 生成高层摘要时使用的低层摘要标识。
        summary_content: 提供给模型的压缩会话内容。
        topics: 摘要提取出的主题标签。
        entities: 摘要提取出的实体名称。
        decisions: 摘要提取出的已确认决策。
        open_items: 摘要提取出的未完成事项。
        model_profile_version: 本次模型调用固定使用的模型配置版本。
        template_version: 生成该内容时使用的模板版本。
        content_hash: 正文的 SHA-256，用于完整性校验、去重与审计。
        status: 当前生命周期状态，决定记录允许的后续操作。
        superseded_by: 替代当前记录的新版本标识；仍有效时为空。
        created_at: 记录创建时间，统一按 UTC 解释。
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
    """定义模型上下文清单Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        manifest_id: 模型上下文清单标识，用于复现单次模型调用输入。
        model_call_id: 一次具体模型调用的关联标识。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        prompt_template_version: 构造模型提示时使用的模板版本。
        agent_profile_version: 本次运行固定使用的 Agent 配置版本。
        model_profile_version: 本次模型调用固定使用的模型配置版本。
        recent_message_start: 本次选择的近期消息起始序号。
        recent_message_end: 本次选择的近期消息结束序号。
        summary_ids: 本次上下文使用的摘要标识。
        memory_ids: 本次上下文实际注入的长期记忆标识，顺序与引用一致。
        memory_refs: 带版本和注入原因的长期记忆引用。
        historical_message_ids: 因相关性被补充选择的较早消息标识。
        tool_result_refs: 上下文引用的外置工具结果标识。
        exposed_tools: 本次模型调用可见的工具名称。
        input_token_count: 最终选择内容的估算输入 token 数。
        available_input_tokens: 扣除输出、系统策略和安全余量后的输入预算。
        omissions: 因预算或相关性未纳入上下文的条目及原因。
        context_hash: 最终上下文选择的稳定哈希，用于审计和复现。
        created_at: 记录创建时间，统一按 UTC 解释。
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
    """定义制品MetadataRow。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        artifact_id: 制品稳定标识。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        content_type: 制品内容的 MIME 类型，供下载方选择解析方式。
        storage_uri: 制品内容的存储位置，不包含访问凭证。
        content_hash: 正文的 SHA-256，用于完整性校验、去重与审计。
        size_bytes: 制品序列化后的字节数。
        source_type: 内容来源类别，例如用户陈述或系统推导。
        source_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        access_policy: 读取制品所需满足的租户、主体或权限限制。
        encryption_metadata: 证明制品静态加密方式的非敏感元数据。
        created_at: 记录创建时间，统一按 UTC 解释。
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
