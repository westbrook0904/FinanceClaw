"""kernel 包的公共出口，汇总跨层共享的稳定契约。

包含执行上下文与数据分级、请求/响应模型以及运行目标（RunTarget）判别联合；
其他分层只允许从本包导入 kernel 契约，保证依赖方向清晰。
"""

from .context import DataClassification, ExecutionContext
from .responses import (
    AgentResponse,
    ApprovalDecision,
    ApprovalDecisionType,
    ArtifactReference,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationTurnAccepted,
    ConversationTurnRequest,
    CreateConversationRequest,
    DirectToolResponse,
    ErrorResponse,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    StreamEvent,
    ToolInvokeRequest,
    WorkflowInvokeRequest,
)
from .targets import AgentTarget, RunTarget, ToolTarget, WorkflowTarget

# 包对外导出的符号清单。
__all__ = [
    "AgentResponse",
    "AgentTarget",
    "ApprovalDecision",
    "ApprovalDecisionType",
    "ArtifactReference",
    "ConversationMessageResponse",
    "ConversationMessagesResponse",
    "ConversationResponse",
    "ConversationTurnAccepted",
    "ConversationTurnRequest",
    "CreateConversationRequest",
    "DataClassification",
    "DirectToolResponse",
    "ErrorResponse",
    "ExecutionContext",
    "RunAccepted",
    "RunRequest",
    "RunStatusResponse",
    "RunTarget",
    "StreamEvent",
    "ToolTarget",
    "ToolInvokeRequest",
    "WorkflowTarget",
    "WorkflowInvokeRequest",
]
