"""执行边界的授权快照、提交关联与根任务预算；不实现图调度或检查点。"""

from .repository import ExecutionConflict, ExecutionRepository, snapshot_context

__all__ = ["ExecutionConflict", "ExecutionRepository", "snapshot_context"]
