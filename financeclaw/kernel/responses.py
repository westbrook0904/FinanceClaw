"""跨层共享且不依赖具体实现的运行上下文、目标与响应契约。"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .targets import RunTarget


class ContractModel(BaseModel):
    """定义契约模型。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid")


class RunRequest(ContractModel):
    """定义运行的接口请求。

    适用场景：
        用于接口层接收并校验调用方输入，再交给应用服务处理。

    属性：
        message: 调用方提交的自然语言消息，是本次规划或会话轮次的主要输入。
        target: 调用方指定的运行目标；为空时使用平台默认根 Agent。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
    """

    message: Annotated[str, Field(min_length=1, max_length=32_000)]
    target: RunTarget | None = None
    conversation_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ConversationTurnRequest(ContractModel):
    """定义会话轮次的接口请求。

    适用场景：
        用于接口层接收并校验调用方输入，再交给应用服务处理。

    属性：
        message: 调用方提交的自然语言消息，是本次规划或会话轮次的主要输入。
    """

    message: Annotated[str, Field(min_length=1, max_length=32_000)]


class ToolInvokeRequest(ContractModel):
    """定义工具Invoke的接口请求。

    适用场景：
        用于接口层接收并校验调用方输入，再交给应用服务处理。

    属性：
        version: 语义版本，用于固定运行行为并支持审计复现。
        arguments: 传给目标工具或工作流的已解析参数。
    """

    version: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkflowInvokeRequest(ContractModel):
    """定义工作流Invoke的接口请求。

    适用场景：
        用于接口层接收并校验调用方输入，再交给应用服务处理。

    属性：
        version: 语义版本，用于固定运行行为并支持审计复现。
        arguments: 传给目标工具或工作流的已解析参数。
    """

    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class RunAccepted(ContractModel):
    """定义运行Accepted。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        status: 当前生命周期状态，决定记录允许的后续操作。
        target_kind: 实际运行目标类别，用于调用方解释运行语义。
        idempotent_replay: 本次结果是否来自相同幂等键和请求内容的安全重放。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
    """

    run_id: str
    thread_id: str
    status: str
    target_kind: str
    idempotent_replay: bool = False
    conversation_id: str | None = None
    turn_id: str | None = None


class ConversationTurnAccepted(ContractModel):
    """定义会话轮次Accepted。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        status: 当前生命周期状态，决定记录允许的后续操作。
        idempotent_replay: 本次结果是否来自相同幂等键和请求内容的安全重放。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
    """

    run_id: str
    status: str
    idempotent_replay: bool = False
    conversation_id: str
    turn_id: str


class CreateConversationRequest(ContractModel):
    """定义创建会话的接口请求。

    适用场景：
        用于接口层接收并校验调用方输入，再交给应用服务处理。
    """


class ConversationResponse(ContractModel):
    """定义会话的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        status: 当前生命周期状态，决定记录允许的后续操作。
        created_at: 记录创建时间，统一按 UTC 解释。
    """

    conversation_id: str
    status: str
    created_at: str


class ConversationMessageResponse(ContractModel):
    """定义会话消息的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        message_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        sequence: 消息在会话内从 1 开始的稳定顺序号。
        parent_message_id: 被该消息直接响应的父消息标识；无父消息时为空。
        role: 消息发送方角色。
        content: 经过边界校验后保存或传递的正文内容。
        created_at: 记录创建时间，统一按 UTC 解释。
    """

    message_id: str
    turn_id: str
    sequence: int
    parent_message_id: str | None = None
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class ConversationMessagesResponse(ContractModel):
    """定义会话Messages的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        messages: 按会话顺序排列的消息集合。
    """

    conversation_id: str
    messages: tuple[ConversationMessageResponse, ...]


class RunStatusResponse(ContractModel):
    """定义运行状态的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        status: 当前生命周期状态，决定记录允许的后续操作。
        output: 运行完成后的结构化输出；尚未完成时为空。
    """

    run_id: str
    thread_id: str
    status: str
    output: dict[str, Any] | list[Any] | None = None


class AgentResponse(ContractModel):
    """定义Agent的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        status: 当前生命周期状态，决定记录允许的后续操作。
        message: 调用方提交的自然语言消息，是本次规划或会话轮次的主要输入。
    """

    run_id: str
    thread_id: str
    status: str
    message: str | None = None


class ArtifactReference(ContractModel):
    """定义外置制品的位置、大小和完整性元数据。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        artifact_id: 制品稳定标识。
        content_type: 制品内容的 MIME 类型，供下载方选择解析方式。
        content_hash: 正文的 SHA-256，用于完整性校验、去重与审计。
        size_bytes: 制品序列化后的字节数。
    """

    artifact_id: str
    content_type: str
    content_hash: str
    size_bytes: int = Field(ge=0)


class DirectToolResponse(ContractModel):
    """定义直接工具的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tool_id: 工具的稳定标识。
        tool_version: 运行固定使用的版本，用于审计复现。
        status: 当前生命周期状态，决定记录允许的后续操作。
        result: 内部步骤产生、等待后续投影的执行结果。
        error: 失败原因的稳定文本；成功或未结束时为空。
        artifact: 详细报告的制品引用；未生成时为空。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
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
    """定义审批决策Type。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        APPROVE: 审批人同意按原参数继续执行。
        REJECT: 审批人拒绝执行并结束当前动作。
        EDIT: 审批人提供修改后的参数，再按新参数重新授权。
    """

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ApprovalDecision(ContractModel):
    """定义审批决策。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        type: 流事件类型，决定客户端如何解释 `data` 载荷。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        arguments: 传给目标工具或工作流的已解析参数。
        reason: 产生当前决策、遗漏或状态的可读原因。
    """

    type: ApprovalDecisionType
    arguments_hash: str | None = None
    arguments: dict[str, Any] | None = None
    reason: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> "ApprovalDecision":
        """校验审批决策的跨字段一致性；不满足不变量时拒绝构造。"""
        if self.type is ApprovalDecisionType.EDIT and self.arguments is None:
            raise ValueError("edit approval decision requires arguments")
        if self.type is not ApprovalDecisionType.EDIT and self.arguments is not None:
            raise ValueError("arguments are only valid for edit decisions")
        return self


class StreamEvent(ContractModel):
    """定义Stream事件。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        event: SSE 事件名称，供客户端选择对应处理分支。
        data: 已经过 JSON 序列化的流事件载荷。
    """

    event: str
    data: Any


class ErrorResponse(ContractModel):
    """定义Error的边界响应。

    适用场景：
        用于接口层向调用方返回稳定结构，避免泄露内部实现对象。

    属性：
        code: 稳定错误代码，供客户端分支处理而不依赖提示文本。
        message: 调用方提交的自然语言消息，是本次规划或会话轮次的主要输入。
        details: 可安全返回给调用方的结构化错误详情。
    """

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
