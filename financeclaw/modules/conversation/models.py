"""定义会话、消息、摘要和模型上下文清单领域记录。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenRecord(BaseModel):
    """定义不可变的持久化记录。

    适用场景：
        用于跨步骤保存不可变事实，并支持持久化或审计重放。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationStatus(StrEnum):
    """会话是否仍允许继续追加轮次。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ACTIVE: 记录当前有效，可继续读取或追加操作。
        ARCHIVED: 记录已归档，只保留历史查询用途。
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    """会话轮次从接收到完成或失败的生命周期状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ACCEPTED: 表示 `accepted` 这一受限枚举值。
        PENDING: 操作已创建但尚未开始处理。
        RUNNING: 操作正在执行且尚未产生终态结果。
        WAITING_CHILD: 父运行暂停推进，正在等待子委派完成。
        INTERRUPTED: 运行停在可恢复检查点，等待外部决定。
        COMPLETED: 操作已成功完成并可读取最终结果。
        FAILED: 操作已失败，错误信息应记录在对应字段。
    """

    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CHILD = "waiting_child"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageRole(StrEnum):
    """消息在对话中的发送方角色。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        USER: 消息来自已认证用户。
        ASSISTANT: 消息由 Agent 或模型生成。
    """

    USER = "user"
    ASSISTANT = "assistant"


class SummaryStatus(StrEnum):
    """摘要是否仍是当前有效版本。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ACTIVE: 记录当前有效，可继续读取或追加操作。
        SUPERSEDED: 该版本已被新版本替代，不再作为当前有效记录。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class Conversation(FrozenRecord):
    """定义一条根 Agent 会话。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        agent_id: Agent 配置的稳定标识。
        agent_profile_version: 本次运行固定使用的 Agent 配置版本。
        agent_thread_id: 根 Agent 使用的服务端线程标识。
        status: 当前生命周期状态，决定记录允许的后续操作。
        created_at: 记录创建时间，统一按 UTC 解释。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
    """

    conversation_id: str
    tenant_id: str
    subject_id: str
    agent_id: str
    agent_profile_version: str
    agent_thread_id: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime
    updated_at: datetime


class ConversationTurn(FrozenRecord):
    """定义会话中的一次用户轮次及运行关联。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
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
    """

    turn_id: str
    conversation_id: str
    tenant_id: str
    subject_id: str
    run_id: str
    server_run_id: str | None = None
    client_idempotency_key: str
    request_hash: str
    target_type: str
    target_id: str
    target_version: str
    status: TurnStatus
    created_at: datetime
    completed_at: datetime | None = None


class ConversationMessage(FrozenRecord):
    """定义会话 Journal 中的一条有序消息。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
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
    """

    message_id: str
    conversation_id: str
    turn_id: str
    sequence: int = Field(ge=1)
    parent_message_id: str | None = None
    role: MessageRole
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible: bool = True
    created_at: datetime


class ConversationSummary(FrozenRecord):
    """定义一段连续消息或低层摘要的压缩表示。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
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

    summary_id: str
    conversation_id: str
    level: int = Field(ge=0)
    start_sequence: int = Field(ge=1)
    end_sequence: int = Field(ge=1)
    source_message_ids: tuple[str, ...] = ()
    source_summary_ids: tuple[str, ...] = ()
    summary_content: str
    topics: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    model_profile_version: str
    template_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: SummaryStatus = SummaryStatus.ACTIVE
    superseded_by: str | None = None
    created_at: datetime


class ContextOmission(FrozenRecord):
    """定义上下文Omission。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        reason: 产生当前决策、遗漏或状态的可读原因。
        item_type: 被上下文选择算法省略的条目类别。
        item_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        token_count: 该条目占用的估算 token 数。
    """

    reason: Literal[
        "token_budget",
        "recent_window",
        "not_relevant",
        "artifact_offloaded",
        "current_input_truncated",
    ]
    item_type: Literal["message", "summary", "tool_result", "current_input"]
    item_id: str
    token_count: int = Field(ge=0)


class ManifestMemoryReference(FrozenRecord):
    """定义清单记忆的稳定引用。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        memory_id: 长期记忆稳定标识。
        schema_version: 记录结构版本，用于兼容演进和历史数据解析。
        memory_type: 长期记忆的语义类别。
        injection_reason: 该记忆与当前问题相关并被注入的原因。
    """

    memory_id: str
    schema_version: int = Field(ge=1)
    memory_type: Literal["preference", "goal", "constraint", "decision_note"]
    injection_reason: str


class ModelContextManifest(FrozenRecord):
    """定义一次模型调用实际可见内容的审计清单。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
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

    manifest_id: str
    model_call_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    prompt_template_version: str
    agent_profile_version: str
    model_profile_version: str
    recent_message_start: int | None = None
    recent_message_end: int | None = None
    summary_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    memory_refs: tuple[ManifestMemoryReference, ...] = ()
    historical_message_ids: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    exposed_tools: tuple[str, ...] = ()
    input_token_count: int = Field(ge=0)
    available_input_tokens: int = Field(ge=1)
    omissions: tuple[ContextOmission, ...] = ()
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def memory_ids_must_match_versioned_references(self) -> Self:
        """校验一次模型调用实际可见内容的审计清单的跨字段一致性；不满足不变量时拒绝构造。"""
        referenced_ids = tuple(item.memory_id for item in self.memory_refs)
        if self.memory_ids != referenced_ids:
            raise ValueError("manifest memory_ids must match memory_refs in order")
        if len(referenced_ids) != len(set(referenced_ids)):
            raise ValueError("manifest memory references must be unique")
        return self


class ContextSelection(FrozenRecord):
    """定义上下文选择算法输出的可序列化证据。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        recent_message_ids: 关联对象标识的有序集合。
        summary_ids: 本次上下文使用的摘要标识。
        memory_refs: 带版本和注入原因的长期记忆引用。
        historical_message_ids: 因相关性被补充选择的较早消息标识。
        tool_result_refs: 上下文引用的外置工具结果标识。
        input_token_count: 最终选择内容的估算输入 token 数。
        available_input_tokens: 扣除输出、系统策略和安全余量后的输入预算。
        omissions: 因预算或相关性未纳入上下文的条目及原因。
        context_hash: 最终上下文选择的稳定哈希，用于审计和复现。
        debug_payload: 仅调试模式保存的上下文明细；正常模式保持为空。
    """

    recent_message_ids: tuple[str, ...] = ()
    summary_ids: tuple[str, ...] = ()
    memory_refs: tuple[ManifestMemoryReference, ...] = ()
    historical_message_ids: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    input_token_count: int
    available_input_tokens: int
    omissions: tuple[ContextOmission, ...] = ()
    context_hash: str
    debug_payload: dict[str, Any] = Field(default_factory=dict)
