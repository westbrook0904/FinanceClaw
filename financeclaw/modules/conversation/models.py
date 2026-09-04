"""会话日志模块的领域模型（Pydantic 冻结记录）与状态枚举定义。

定义 Conversation/Turn/Message/Summary/Manifest 等不可变记录，作为
context、repository、summaries 与上层服务共享的数据契约。
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenRecord(BaseModel):
    """所有会话领域记录共用的冻结基类。

    使用场景：作为 Conversation、ConversationMessage 等记录模型的基类，统一
    禁止未知字段与就地修改，保证写入业务数据库的记录不可变。

    Attributes:
        model_config: Pydantic 模型配置；extra="forbid" 拒绝未知字段，
            frozen=True 禁止修改实例属性。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationStatus(StrEnum):
    """会话的生命周期状态。

    使用场景：创建会话时默认置为 ACTIVE，归档后置为 ARCHIVED；
    归档会话不再接受新 turn。

    成员（StrEnum 取值即入库字符串）：
        ACTIVE：会话处于活跃状态，可继续追加 turn 与消息。
        ARCHIVED：会话已归档，只读，不再接受新消息。
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    """一次对话 turn 的执行状态。

    使用场景：BFF 创建 turn 时置为 ACCEPTED，Agent Server 认领后流转到
    RUNNING/WAITING_CHILD 等中间态，最终收敛到 COMPLETED 或 FAILED；
    非终态 turn 可被对账流程扫描并跨 Agent Server 重启继续。

    成员（StrEnum 取值即入库字符串）：
        ACCEPTED：BFF 已受理并写入用户消息，尚未被 Agent Server 认领。
        PENDING：状态归一化时的兜底取值，表示等待执行。
        RUNNING：Agent Server 正在执行该 turn。
        WAITING_CHILD：正在等待子 Agent 或子流程返回。
        INTERRUPTED：执行被中断，尚未到达终态。
        COMPLETED：执行成功，assistant 回复已落库。
        FAILED：执行失败，turn 进入终态。
    """

    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CHILD = "waiting_child"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageRole(StrEnum):
    """对话原文消息的角色枚举。

    使用场景：Conversation Journal 只持久化 user 与 assistant 两类原文，
    工具往返等中间内容不进入原文日志。

    成员（StrEnum 取值即入库字符串）：
        USER：用户输入的消息。
        ASSISTANT：Agent 产生的最终回复消息。
    """

    USER = "user"
    ASSISTANT = "assistant"


class SummaryStatus(StrEnum):
    """摘要的生命周期状态。

    使用场景：摘要生成后为 ACTIVE；被重建版本替换后置为 SUPERSEDED 并记录
    superseded_by，旧摘要保留供审计但不再进入上下文选择。

    成员（StrEnum 取值即入库字符串）：
        ACTIVE：当前生效、可被上下文装配选用的摘要。
        SUPERSEDED：已被更新版本取代的历史摘要。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class Conversation(FrozenRecord):
    """一次持久化对话的聚合根记录，固定绑定租户、主体与 Agent 线程。

    使用场景：BFF 创建会话时写入；后续所有 turn、消息、摘要与 Manifest 都通过
    conversation_id 关联到该记录，agent_thread_id 唯一，支撑跨重启继续。

    Attributes:
        conversation_id: 会话全局唯一标识，形如 "conversation-<hex>"，作为主键。
        tenant_id: 租户标识，用于多租户隔离与归属校验。
        subject_id: 主体（用户或服务账号）标识，与 tenant_id 共同构成归属键。
        agent_id: 绑定的 Agent 标识，会话内所有模型调用共享同一 Agent。
        agent_profile_version: 创建会话时的 Agent Profile 版本，用于审计与兼容。
        agent_thread_id: LangGraph Agent 线程 ID（UUID 字符串），全局唯一。
        status: 会话状态，默认 ACTIVE，取值见 ConversationStatus。
        created_at: 会话创建时间（UTC）。
        updated_at: 会话最近一次更新时间（UTC），通常随新消息追加而刷新。

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


class ChannelConversationBinding(FrozenRecord):
    """外部消息单聊与 FinanceClaw Conversation 的稳定绑定。

    使用场景：飞书 Channel 按应用、租户和 P2P chat 定位唯一会话，保证多轮
    消息复用同一个 Agent thread；绑定身份只来自 SDK 已验证事件。

    Attributes:
        binding_id: 绑定记录主键。
        channel: Channel 类型，一期固定为 ``feishu``。
        app_id: 飞书应用 ID（非 Secret）。
        tenant_key: 飞书事件中的租户标识。
        external_user_id: 发件人的飞书 open_id。
        external_chat_id: 飞书 P2P chat_id。
        conversation_id: 关联的 FinanceClaw Conversation ID。
        created_at: 绑定创建时间（UTC）。
        updated_at: 最近一次解析绑定的时间（UTC）。

    """

    binding_id: str
    channel: str
    app_id: str
    tenant_key: str
    external_user_id: str
    external_chat_id: str
    conversation_id: str
    created_at: datetime
    updated_at: datetime


class ConversationTurn(FrozenRecord):
    """一次对话轮次的执行记录，承载幂等键与 Agent Server 运行绑定。

    使用场景：BFF 以 client_idempotency_key 幂等创建 turn 并写入用户消息；
    Agent Server 通过 bind_server_run 绑定 server_run_id，重启后按状态对账续跑。

    Attributes:
        turn_id: turn 全局唯一标识，形如 "turn-<hex>"，作为主键。
        conversation_id: 所属会话标识，关联 Conversation。
        tenant_id: 租户标识，与 subject_id 共同用于归属校验。
        subject_id: 主体标识，与 tenant_id、client_idempotency_key 构成幂等约束。
        run_id: 平台侧运行标识，形如 "run-<hex>"，Agent Server 由此定位 turn。
        server_run_id: Agent Server 侧运行标识；一个 turn 至多绑定一个，未绑定为 None。
        client_idempotency_key: 客户端幂等键，同租户同主体下唯一，防止重复提交。
        request_hash: 请求内容哈希，用于检测幂等键复用但请求不同的情况。
        target_type: 目标对象类型（被操作的领域对象类别）。
        target_id: 目标对象标识。
        target_version: 目标对象版本，用于并发与兼容控制。
        status: turn 当前状态，取值见 TurnStatus。
        created_at: turn 创建时间（UTC）。
        completed_at: turn 到达终态的时间（UTC）；未完成时为 None。

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
    """对话原文日志中的单条消息，append-only 且按 sequence 排序。

    使用场景：turn 创建时写入用户消息，执行完成后写入 assistant 回复；
    上下文装配时按预算从中选取最近原文与相关古老历史消息。

    Attributes:
        message_id: 消息全局唯一标识，形如 "message-<hex>"，作为主键。
        conversation_id: 所属会话标识，关联 Conversation。
        turn_id: 产生该消息的 turn 标识，关联 ConversationTurn。
        sequence: 会话内序号（从 1 起递增），与会话共同唯一，决定上下文顺序。
        parent_message_id: 父消息标识（分支消息用）；顶层消息为 None。
        role: 消息角色，取值见 MessageRole（仅 user/assistant）。
        content: 消息原文文本。
        content_hash: 内容的 SHA-256 十六进制摘要，用于幂等对账与冲突检测。
        visible: 是否对用户可见；默认 True，隐藏消息不参与默认上下文装配。
        created_at: 消息写入时间（UTC）。

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
    """一段历史消息或低层摘要的摘要记录，支持分段与分层两种粒度。

    使用场景：SummaryService 按分段（level=0）与分层（level>=1）生成摘要并落库；
    上下文装配时按相关性选取摘要，替代超出预算的古老原文。

    Attributes:
        summary_id: 摘要全局唯一标识，形如 "summary-<hex>"，作为主键。
        conversation_id: 所属会话标识，关联 Conversation。
        level: 摘要层级；0 表示由原文消息生成的分段摘要，>=1 表示聚合低层摘要。
        start_sequence: 覆盖范围的起始消息序号（含）。
        end_sequence: 覆盖范围的结束消息序号（含）。
        source_message_ids: 生成该摘要的源消息 ID 元组；level=0 时使用。
        source_summary_ids: 生成该摘要的源摘要 ID 元组；level>=1 时使用。
        summary_content: 摘要正文文本。
        topics: 摘要覆盖的主题词元组，用于相关性排序。
        entities: 摘要中的实体（如股票代码）元组，用于相关性排序。
        decisions: 摘要记录的历史决策元组，默认为空。
        open_items: 摘要遗留的未决事项元组，默认为空。
        model_profile_version: 生成摘要所用摘要器/模型配置版本。
        template_version: 摘要模板版本，用于审计与再生成兼容。
        content_hash: 摘要内容的 SHA-256 十六进制摘要，用于重建幂等判定。
        status: 摘要状态，默认 ACTIVE，取值见 SummaryStatus。
        superseded_by: 取代该摘要的新摘要 ID；未被取代时为 None。
        created_at: 摘要创建时间（UTC）。

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
    """上下文装配中被省略项的记录，用于审计与调试 token 取舍。

    使用场景：ConversationContextBuilder 在预算不足或工件外置时生成省略记录，
    随 Manifest 持久化，说明每条历史为何没有进入本次模型调用。

    Attributes:
        reason: 省略原因；token_budget=超出预算、recent_window=超出最近窗口、
            not_relevant=与查询无关、artifact_offloaded=工具结果已外置、
            current_input_truncated=当前输入被截断。
        item_type: 被省略对象的类型；message/summary/tool_result/current_input。
        item_id: 被省略对象的标识（消息 ID、摘要 ID 或运行时占位 ID）。
        token_count: 该对象占用的 token 估算值（截断场景为被裁掉的数量）。

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
    """进入模型上下文的一条记忆引用，说明注入了哪条记忆及其原因。

    使用场景：记忆中间件注入偏好/目标等记忆时生成引用，随 Manifest 持久化，
    保证模型调用中记忆使用的可追溯性。

    Attributes:
        memory_id: 被注入记忆的唯一标识。
        schema_version: 记忆条目的 schema 版本，从 1 起。
        memory_type: 记忆类型；preference=偏好、goal=目标、constraint=约束、
            decision_note=决策备注。
        injection_reason: 注入该记忆的原因说明。

    """

    memory_id: str
    schema_version: int = Field(ge=1)
    memory_type: Literal["preference", "goal", "constraint", "decision_note"]
    injection_reason: str


class ModelContextManifest(FrozenRecord):
    """一次模型调用的上下文清单，永久记录本次调用实际使用的上下文构成。

    使用场景：每次模型调用前由上下文中间件构建并经 save_manifest 持久化，
    支撑审计、成本分析与跨 Agent Server 重启后的行为复现。

    Attributes:
        manifest_id: 清单唯一标识，形如 "manifest-<hex>"。
        model_call_id: 模型调用唯一标识，形如 "model-call-<hex>"，同调用幂等。
        conversation_id: 所属会话标识。
        turn_id: 所属 turn 标识。
        run_id: 平台侧运行标识。
        prompt_template_version: 所用提示词模板版本。
        agent_profile_version: 所用 Agent Profile 版本。
        model_profile_version: 所用模型配置版本。
        recent_message_start: 入选最近原文消息的最小序号；无入选时为 None。
        recent_message_end: 入选最近原文消息的最大序号；无入选时为 None。
        summary_ids: 本次入选的摘要 ID 元组。
        memory_ids: 注入的记忆 ID 元组，必须与 memory_refs 顺序一致。
        memory_refs: 记忆引用明细，见 ManifestMemoryReference。
        historical_message_ids: 按相关性入选的古老历史消息 ID 元组。
        tool_result_refs: 本次调用涉及的外置工件（工具结果）ID 元组。
        exposed_tools: 本次暴露给模型的工具清单（带版本的工具 ID）。
        input_token_count: 输入 token 估算总数（含系统提示与工具 schema）。
        available_input_tokens: 配置的可用输入 token 预算。
        omissions: 被省略项明细，见 ContextOmission。
        context_hash: 完整输入上下文的 SHA-256 摘要，用于幂等与一致性校验。
        created_at: 清单创建时间（UTC），默认当前时间。

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
        """校验 memory_ids 与 memory_refs 严格一一对应且无重复。

        使用场景：构造 Manifest 时自动执行，防止清单中记忆 ID 与引用明细不一致。

        Returns:
            Self: 校验通过后的模型实例。

        Raises:
            ValueError: memory_ids 与 memory_refs 不一致或存在重复记忆 ID 时抛出。

        """
        referenced_ids = tuple(item.memory_id for item in self.memory_refs)
        if self.memory_ids != referenced_ids:
            raise ValueError("manifest memory_ids must match memory_refs in order")
        if len(referenced_ids) != len(set(referenced_ids)):
            raise ValueError("manifest memory references must be unique")
        return self


class ContextSelection(FrozenRecord):
    """一次上下文装配的选取结果，是构建 ModelContextManifest 的直接数据来源。

    使用场景：ConversationContextBuilder.build 返回该对象；中间件据此组装最终
    消息列表、构建 Manifest，并在 development 环境输出完整 Prompt 调试信息。

    Attributes:
        recent_message_ids: 按 token 预算入选的最近原文消息 ID 元组（按会话顺序）。
        summary_ids: 入选的摘要 ID 元组。
        memory_refs: 注入的记忆引用元组。
        historical_message_ids: 按相关性入选的古老历史消息 ID 元组（按序号升序）。
        tool_result_refs: 被外置的工件（工具结果）ID 元组。
        input_token_count: 本次装配的输入 token 估算总数（含系统提示与工具 schema）。
        available_input_tokens: 配置的可用输入 token 预算。
        omissions: 被省略项明细元组。
        context_hash: 完整输入上下文的 SHA-256 摘要。
        debug_payload: 调试负载，含系统提示、消息序列化结果、工具 schema 与
            预算配置；仅在 development 环境用于输出完整 Prompt。

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
