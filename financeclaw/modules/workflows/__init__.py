"""工作流模块的公开出口：聚合流程目录、定义模型与运行审批仓储。

提供启动期装配的不可变 WorkflowCatalog，以及业务 run、Agent Server thread、
审批决定与发布制品映射的持久化能力。
"""

from .catalog import WorkflowCatalog, WorkflowCatalogError
from .models import (
    ApprovalPoint,
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStatus,
    WorkflowTimeoutPolicy,
    WorkflowToolRef,
)
from .repository import (
    SqlAlchemyWorkflowRepository,
    WorkflowConflict,
    WorkflowIdempotencyConflict,
    WorkflowNotFound,
    WorkflowRepository,
)

# 模块公开接口清单。
__all__ = [
    "ApprovalPoint",
    "SqlAlchemyWorkflowRepository",
    "WorkflowApproval",
    "WorkflowApprovalStatus",
    "WorkflowCatalog",
    "WorkflowCatalogError",
    "WorkflowConflict",
    "WorkflowDefinition",
    "WorkflowIdempotencyConflict",
    "WorkflowNotFound",
    "WorkflowRepository",
    "WorkflowRun",
    "WorkflowRunStatus",
    "WorkflowStatus",
    "WorkflowTimeoutPolicy",
    "WorkflowToolRef",
]
