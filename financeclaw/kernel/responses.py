"""跨层共享的请求/响应契约模型，覆盖根运行受理、会话轮次与只读投影。

本模块属于 kernel（稳定共享契约层）：API 与共享持久化设施据此
收发数据；所有模型均继承 ``ContractModel``，禁止未声明的额外字段。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    """全部对外契约模型的公共基类，统一禁止未声明字段。

    使用场景：本模块内的请求/响应模型均继承它，使 API 层在遇到契约外
    字段时直接校验失败，避免未知字段静默穿透到业务层。
    """

    model_config = ConfigDict(extra="forbid")


class ConversationTurnRequest(ContractModel):
    """创建 message-only Turn 的请求体，即 API 唯一产品写入口的入参。

    使用场景：终端用户在会话中发言时，API 用它创建 Conversation + Turn，
    由 finance_agent 决定直接回答、调用能力或子图调用。

    Attributes:
        message: 用户消息正文，长度 1~32000 字符。

    """

    message: Annotated[str, Field(min_length=1, max_length=32_000)]


class TurnAccepted(ContractModel):
    """Durably accepted input; native admission may still be pending."""

    conversation_id: str
    turn_id: str
    status: str
    revision: int
    idempotent_replay: bool = False


class CreateConversationRequest(ContractModel):
    """创建会话的请求体。

    使用场景：客户端开启新会话时使用；当前无需任何入参，保留空模型
    作为契约占位，便于未来扩展字段而不破坏兼容性。
    """

    pass


class ConversationResponse(ContractModel):
    """会话基础信息的响应体。

    使用场景：创建会话成功后返回，客户端保存 ``conversation_id`` 用于
    后续发言与查询。

    Attributes:
        conversation_id: 会话全局唯一 ID。
        status: 会话状态字符串，由服务端状态机定义。
        created_at: 会话创建时间（ISO 格式字符串）。

    """

    conversation_id: str
    status: str
    created_at: str


class ConversationMessageResponse(ContractModel):
    """会话内单条消息的响应体。

    使用场景：查询会话消息列表时返回，客户端依据 ``sequence`` 还原顺序、
    依据 ``parent_message_id`` 还原分支关系。

    Attributes:
        message_id: 消息全局唯一 ID。
        turn_id: 消息所属的轮次 ID。
        sequence: 消息在会话内的序号，单调递增，用于排序。
        parent_message_id: 父消息 ID，用于分支/重试场景；无父消息时为 None。
        role: 消息角色，仅允许 ``user`` 或 ``assistant``。
        content: 消息文本内容。
        created_at: 消息创建时间（ISO 格式字符串）。

    """

    message_id: str
    turn_id: str
    sequence: int
    parent_message_id: str | None = None
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class ConversationMessagesResponse(ContractModel):
    """会话消息列表的响应体。

    使用场景：客户端拉取会话历史时返回，通常按 ``sequence`` 升序渲染。

    Attributes:
        conversation_id: 所属会话 ID。
        messages: 会话内全部消息元组，顺序由服务端按会话语义保证。

    """

    conversation_id: str
    messages: tuple[ConversationMessageResponse, ...]


class TurnSnapshot(ContractModel):
    """Safe current product state, independent of native resource identifiers."""

    conversation_id: str
    turn_id: str
    status: str
    revision: int
    reason: str | None = None
    pending_interactions: tuple[dict[str, Any], ...] = ()
    output: dict[str, Any] | None = None
    authorization_revision: int


class ArtifactReference(ContractModel):
    """产出制品（Artifact）的引用信息，指向一个已落盘的制品。

    使用场景：Tool/Workflow 执行产出文件类结果时随响应返回，客户端凭
    ``artifact_id`` 经制品服务获取内容。

    Attributes:
        artifact_id: 制品全局唯一 ID。
        content_type: 制品的 MIME 类型。
        content_hash: 制品内容的哈希值，用于完整性校验。
        size_bytes: 制品字节数，非负整数。

    """

    artifact_id: str
    content_type: str
    content_hash: str
    size_bytes: int = Field(ge=0)


class StreamEvent(ContractModel):
    """流式传输事件的通用信封，包装任意事件类型与负载。

    使用场景：SSE 等流式通道把 Agent/Workflow 的中间事件逐条封装为
    本模型下发，客户端按 ``event`` 分发处理。

    Attributes:
        event: 事件类型名，由服务端事件体系定义。
        data: 事件负载（任意 JSON 值），结构随事件类型而定。

    """

    event: str
    data: Any
    id: str | None = Field(default=None, max_length=256, pattern=r"^[^\r\n\x00]+$")


class ErrorResponse(ContractModel):
    """统一错误响应体：以稳定错误码向客户端描述失败。

    使用场景：接口校验失败或内部异常时返回；客户端依据 ``code`` 做
    程序化处理、依据 ``message`` 做人类可读提示。

    Attributes:
        code: 机器可读的错误码，客户端据此分支处理。
        message: 面向人的错误描述，可直接展示。
        details: 结构化补充信息（如字段级错误），默认为空字典。

    """

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
