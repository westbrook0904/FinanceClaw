"""FinanceClaw 任务委派（delegation）模块的公开接口。

顶层 finance_agent 通过本包把任务委派给 Workflow 或只读领域 Agent：typed
handoff 模型、委派记录仓库、委派工具命名规则与相关异常在此统一导出，供
编排层、应用层与测试共同使用。
"""

from .models import (
    HANDOFF_ADAPTER,
    AgentDelegationInput,
    AgentHandoff,
    DelegationKind,
    DelegationRecord,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    WorkflowHandoff,
)
from .naming import delegation_tool_name
from .repository import (
    DelegationConflict,
    DelegationNotFound,
    DelegationRepository,
    SqlAlchemyDelegationRepository,
)

__all__ = [
    "HANDOFF_ADAPTER",
    "AgentDelegationInput",
    "AgentHandoff",
    "DelegationKind",
    "DelegationConflict",
    "DelegationNotFound",
    "DelegationRecord",
    "DelegationRepository",
    "DelegationResult",
    "DelegationStatus",
    "HandoffRequest",
    "SqlAlchemyDelegationRepository",
    "WorkflowHandoff",
    "delegation_tool_name",
]
