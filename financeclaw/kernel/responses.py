"""跨层共享的请求/响应契约模型，覆盖运行受理、会话轮次、直连调用与审批流。

本模块属于 kernel（稳定共享契约层）：BFF（HTTP 接口层）与 orchestration 据此
收发数据；所有模型均继承 ``ContractModel``，禁止未声明的额外字段。
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .targets import RunTarget


class ContractModel(BaseModel):
    """全部对外契约模型的公共基类，统一禁止未声明字段。

    使用场景：本模块内的请求/响应模型均继承它，使 API 层在遇到契约外
    字段时直接校验失败，避免未知字段静默穿透到业务层。
    """

    model_config = ConfigDict(extra="forbid")


class RunRequest(ContractModel):
    """发起一次 Run 的请求体：不经会话轮次，直接向目标提交任务。

    使用场景：脚本或集成方调用运行入口时使用；不指定 ``target`` 时，
    由顶层 Agent（ReAct）自行决策直接回答、调工具、跑流程或委派。

    Attributes:
        message: 用户消息正文，长度 1~32000 字符。
        target: 可选运行目标；为 None 时由顶层 Agent 自行决策路由。
        conversation_id: 可选会话 ID，用于把本次运行挂到已有会话上下文。

    """

    message: Annotated[str, Field(min_length=1, max_length=32_000)]
    target: RunTarget | None = None
    conversation_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ConversationTurnRequest(ContractModel):
    """创建 message-only Turn 的请求体，即 BFF 唯一产品写入口的入参。

    使用场景：终端用户在会话中发言时，BFF 用它创建 Conversation + Turn，
    由 finance_agent 决定直接回答、调用能力或委派。

    Attributes:
        message: 用户消息正文，长度 1~32000 字符。

    """

    message: Annotated[str, Field(min_length=1, max_length=32_000)]


class ToolInvokeRequest(ContractModel):
    """直连调用 Tool 的请求体：绕过 Agent 决策、直达治理后的工具执行路径。

    使用场景：集成方明确知道要调用的工具时使用；入参校验、策略与审计
    仍由 ToolPolicy 与 AuditRepository 保证。

    Attributes:
        version: 可选工具版本；为 None 时解析为目录中的最新版本。
        arguments: 工具入参字典，默认为空字典，须符合工具参数 schema。

    """

    version: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkflowInvokeRequest(ContractModel):
    """直连调用 Workflow 的请求体：绕过 Agent 决策、直达已发布流程。

    使用场景：集成方需要确定性的多步流程执行时使用；指定版本号以
    保证流程行为可复现。

    Attributes:
        version: 目标 Workflow 的语义化版本号（形如 ``1.2.3``）；可为 None。
        arguments: 流程入参字典，默认为空字典，须符合流程入参 schema。

    """

    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class RunAccepted(ContractModel):
    """Run 受理成功的响应体：返回运行定位信息与初始状态。

    使用场景：运行入口同步受理请求后立即返回，调用方凭 ``run_id``
    查询状态或订阅流式事件。

    Attributes:
        run_id: 受理的运行 ID。
        thread_id: LangGraph 线程 ID，用于状态查询与流式订阅。
        status: 受理时的初始状态字符串，由服务端状态机定义。
        target_kind: 目标类型字符串（tool/workflow/agent）。
        idempotent_replay: True 表示本次为幂等重放，复用了先前同键请求的结果。
        conversation_id: 关联的会话 ID；无会话上下文时为 None。
        turn_id: 关联的轮次 ID；无会话上下文时为 None。

    """

    run_id: str
    thread_id: str
    status: str
    target_kind: str
    idempotent_replay: bool = False
    conversation_id: str | None = None
    turn_id: str | None = None


class ConversationTurnAccepted(ContractModel):
    """会话轮次受理成功的响应体：返回会话、轮次与运行的定位信息。

    使用场景：BFF 创建 message-only Turn 成功后返回，客户端据此轮询
    ``run_id`` 或订阅流式事件以获取 Agent 回复。

    Attributes:
        run_id: 本轮触发的运行 ID。
        status: 受理时的初始状态字符串，由服务端状态机定义。
        idempotent_replay: True 表示本次为幂等重放，复用了先前同键请求的结果。
        conversation_id: 会话 ID。
        turn_id: 本轮次 ID。

    """

    run_id: str
    status: str
    idempotent_replay: bool = False
    conversation_id: str
    turn_id: str


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


class RunStatusResponse(ContractModel):
    """Run 状态查询的响应体。

    使用场景：客户端轮询运行状态时使用；到达终态后 ``output`` 携带
    运行输出。

    Attributes:
        run_id: 被查询的运行 ID。
        thread_id: LangGraph 线程 ID。
        status: 当前状态字符串，由服务端状态机定义。
        output: 运行输出（对象或列表）；尚未产生输出时为 None。

    """

    run_id: str
    thread_id: str
    status: str
    output: dict[str, Any] | list[Any] | None = None


class AgentResponse(ContractModel):
    """Agent 运行的最终响应体：返回自然语言回复与运行定位信息。

    使用场景：非流式调用 Agent 完成后返回；客户端优先展示 ``message``。

    Attributes:
        run_id: 本次运行 ID。
        thread_id: LangGraph 线程 ID，可用于追问与流式订阅。
        status: 运行终态状态字符串，由服务端状态机定义。
        message: Agent 的最终文本回复；无文本产出时为 None。

    """

    run_id: str
    thread_id: str
    status: str
    message: str | None = None


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


class DirectToolResponse(ContractModel):
    """Tool 直连调用的响应体：返回执行状态、结果与治理信息。

    使用场景：直连工具调用结束后返回；调用方依据 ``status`` 分支处理
    成功结果、审批中断或失败原因。

    Attributes:
        run_id: 本次调用对应的运行 ID。
        tool_id: 被调用工具的 ID。
        tool_version: 实际执行的工具版本。
        status: 执行结果状态，仅允许 success/denied/rejected/failed/interrupted。
        result: 工具执行结果（任意 JSON 值）；无结果时为 None。
        error: 失败原因描述；成功时为 None。
        artifact: 工具产出的制品引用；未产出制品时为 None。
        arguments_hash: 入参哈希，用于幂等判定与审计比对；缺失时为 None。

    """

    run_id: str
    tool_id: str
    tool_version: str
    status: Literal["success", "denied", "rejected", "failed", "interrupted"]
    result: Any = None
    error: str | None = None
    artifact: ArtifactReference | None = None
    arguments_hash: str | None = None


class ApprovalDecisionType(StrEnum):
    """审批决策类型枚举，表示人对 Agent 待执行动作的处置方式。

    使用场景：Tool/Workflow 触发人机协同（HITL）审批而挂起时，审批方
    提交 ``ApprovalDecision`` 所用的 ``type`` 字段取值。
    """

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ApprovalDecision(ContractModel):
    """一次人机协同审批的决策内容，决定被挂起动作的后续走向。

    使用场景：Agent 发起需审批的工具/流程调用而挂起等待时，审批人
    通过审批 API 提交本模型以恢复或终止执行。

    Attributes:
        type: 决策类型：批准、驳回或修订入参后重提交。
        arguments_hash: 被审批调用的入参哈希，用于与挂起请求精确匹配。
        arguments: 修订后的入参；仅 ``EDIT`` 决策允许携带。
        reason: 审批理由说明，最长 500 字符；可为 None。

    """

    type: ApprovalDecisionType
    arguments_hash: str | None = None
    arguments: dict[str, Any] | None = None
    reason: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> "ApprovalDecision":
        """校验 ``arguments`` 字段仅允许出现在 ``EDIT`` 决策中。

        Returns:
            校验通过的原模型实例。

        Raises:
            ValueError: ``EDIT`` 决策缺少 ``arguments``，或非 ``EDIT`` 决策
                携带了 ``arguments``。

        """
        if self.type is ApprovalDecisionType.EDIT and self.arguments is None:
            raise ValueError("edit approval decision requires arguments")
        if self.type is not ApprovalDecisionType.EDIT and self.arguments is not None:
            raise ValueError("arguments are only valid for edit decisions")
        return self


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
