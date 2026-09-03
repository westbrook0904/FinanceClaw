"""orchestration.agents 子包：Agent 装配、Agent 档案与 Agent 中间件。

该子包承担 Agent 运行时的全部装配与横切设施：factory 装配顶层 ReAct Agent，
profiles 声明 Agent 档案，各中间件负责上下文组装、记忆召回、工件 offload、
调用偏好指令与工具治理。

"""

from .context_middleware import ConversationContextMiddleware
from .directive_middleware import InvocationDirectiveMiddleware
from .directives import (
    InvocationDirective,
    InvocationKind,
    SlotAssessment,
    assess_tool_slots,
    parse_invocation_directive,
)
from .factory import AgentFactory
from .offline import OfflineFinanceModel
from .profiles import AgentProfile, AgentProfileCatalog, ToolRef

__all__ = [
    "AgentFactory",
    "AgentProfile",
    "AgentProfileCatalog",
    "ConversationContextMiddleware",
    "InvocationDirective",
    "InvocationDirectiveMiddleware",
    "InvocationKind",
    "OfflineFinanceModel",
    "SlotAssessment",
    "assess_tool_slots",
    "parse_invocation_directive",
    "ToolRef",
]
