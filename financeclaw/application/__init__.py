"""应用层用例服务的统一出口：向 BFF 与引导装配暴露跨模块用例、异常与出站 Port。"""

from .conversation_service import ApprovalExpired, ConversationService
from .delegation_service import (
    DelegationAuthorizationError,
    DelegationInputError,
    DelegationService,
    delegation_projection,
    extract_handoff_interrupt,
)
from .feishu_channel_service import (
    FeishuChannelService,
    FeishuInboundMessage,
    FeishuMarkdownStream,
    FeishuReplyGateway,
)
from .ports import AgentServerClient, ServerRun
from .run_service import IdempotencyConflict, RunNotFound, RunService
from .target_resolver import ResolvedTarget, TargetResolutionError, TargetResolver
from .workflow_service import (
    WorkflowApprovalExpired,
    WorkflowAuthorizationError,
    WorkflowInputError,
    WorkflowService,
)

# 公开 API 清单：限定包级 `import *` 与外部引用的可见符号。
__all__ = [
    "AgentServerClient",
    "ApprovalExpired",
    "ConversationService",
    "DelegationAuthorizationError",
    "DelegationInputError",
    "DelegationService",
    "FeishuChannelService",
    "FeishuInboundMessage",
    "FeishuMarkdownStream",
    "FeishuReplyGateway",
    "IdempotencyConflict",
    "ResolvedTarget",
    "RunNotFound",
    "RunService",
    "ServerRun",
    "TargetResolutionError",
    "TargetResolver",
    "WorkflowApprovalExpired",
    "WorkflowAuthorizationError",
    "WorkflowInputError",
    "WorkflowService",
    "delegation_projection",
    "extract_handoff_interrupt",
]
