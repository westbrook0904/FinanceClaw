"""执行事实模块：授权快照、出站操作关联、根预算与取消状态。

repository 提供短事务与原子状态更新；application.execution_service 负责远程提交
和回执对账；orchestration 中的预算中间件在真实调用前消费额度。本包不调度图节点，
也不管理 LangGraph checkpoint。与 delegation 的跨表写入是当前明确的事务耦合。
"""

from .repository import ExecutionConflict, ExecutionRepository, snapshot_context

__all__ = ["ExecutionConflict", "ExecutionRepository", "snapshot_context"]
