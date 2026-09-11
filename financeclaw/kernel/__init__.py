"""kernel 包的公共出口，汇总跨层共享的稳定契约。

其他分层只允许从本包导入 kernel 契约，保证依赖方向清晰。
"""

from financeclaw.kernel.context import DataClassification, ExecutionContext
from financeclaw.kernel.responses import (
    ArtifactReference,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationTurnRequest,
    CreateConversationRequest,
    ErrorResponse,
    StreamEvent,
    TurnAccepted,
    TurnSnapshot,
)

# 包对外导出的符号清单。
__all__ = [
    "ArtifactReference",
    "ConversationMessageResponse",
    "ConversationMessagesResponse",
    "ConversationResponse",
    "ConversationTurnRequest",
    "CreateConversationRequest",
    "DataClassification",
    "ErrorResponse",
    "ExecutionContext",
    "TurnAccepted",
    "TurnSnapshot",
    "StreamEvent",
]
