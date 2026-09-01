"""ExecutionPlan 持久化与恢复使用的稳定快照协议。"""

from __future__ import annotations

from pydantic import model_validator

from .base import ContractModel, NonEmptyString
from .context import InvocationContext
from .execution import (
    NodeExecutionState,
    NodeExecutionStatus,
    PlanExecutionState,
    PlanExecutionStatus,
)
from .exploration import ExplorationState, ExplorationStatus
from .plan import ExecutionPlan, PlanNodeKind
from .result import ResultStatus


class PlanExecutionRecord(ContractModel):
    """StateStore 原子保存的完整 Plan 执行快照。

    ``InvocationContext`` 已包含原始 Request，因此无需在 Record 中再保存一份重复
    Request。CancellationSignal、asyncio Task、Provider 和连接对象属于进程内资源，
    不得进入本协议；这里只保存 ``CancellationContext`` 等可序列化快照。
    """

    plan_id: NonEmptyString
    plan: ExecutionPlan
    context: InvocationContext
    state: PlanExecutionState

    @model_validator(mode="after")
    def validate_consistency(self) -> PlanExecutionRecord:
        """防止主键、静态 Plan 和可变 State 指向不同的执行实例。"""

        if self.plan.plan_id != self.plan_id:
            raise ValueError("record plan_id must match plan.plan_id")
        if self.state.plan_id != self.plan_id:
            raise ValueError("record plan_id must match state.plan_id")
        if self.state.plan_revision != self.plan.revision:
            raise ValueError("state plan_revision must match plan.revision")
        self._validate_explorations()
        return self

    def _validate_explorations(self) -> None:
        exploration_nodes = {
            node.node_id: node for node in self.plan.nodes if node.kind is PlanNodeKind.EXPLORATION
        }
        for map_key, exploration in self.state.explorations.items():
            if map_key != exploration.node_id:
                raise ValueError("exploration map key must match child node_id")
            node = exploration_nodes.get(map_key)
            if node is None or node.exploration is None:
                raise ValueError("exploration state must reference an exploration node")
            if exploration.plan_id != self.plan_id:
                raise ValueError("exploration state plan_id must match record")
            if exploration.exploration_id != node.exploration.exploration_id:
                raise ValueError("exploration state identity must match node spec")
            if exploration.profile != node.exploration.profile:
                raise ValueError("exploration state profile must match node snapshot")

            outer = self.state.nodes.get(map_key)
            if outer is None:
                raise ValueError("exploration state requires outer node state")
            self._validate_outer_exploration(exploration, outer)

        for node_id in exploration_nodes:
            outer = self.state.nodes.get(node_id)
            if outer is None:
                raise ValueError("exploration plan requires outer node state")
            if node_id not in self.state.explorations and outer.status not in {
                NodeExecutionStatus.PENDING,
                NodeExecutionStatus.READY,
            }:
                raise ValueError("started exploration node requires child state")

    def _validate_outer_exploration(
        self,
        exploration: ExplorationState,
        outer: NodeExecutionState,
    ) -> None:
        if exploration.status in {ExplorationStatus.CREATED, ExplorationStatus.RUNNING}:
            if outer.status is not NodeExecutionStatus.RUNNING:
                raise ValueError("active exploration requires RUNNING outer node")
            if exploration.final_result is not None or outer.result is not None:
                raise ValueError("active exploration forbids terminal result")
            if exploration.completed_at is not None or outer.completed_at is not None:
                raise ValueError("active exploration forbids completed_at")
            if self.state.status is not PlanExecutionStatus.RUNNING:
                raise ValueError("active standalone exploration requires RUNNING plan")
            return

        terminal = {
            ExplorationStatus.SUCCEEDED: (
                NodeExecutionStatus.SUCCEEDED,
                PlanExecutionStatus.SUCCEEDED,
                ResultStatus.SUCCESS,
            ),
            ExplorationStatus.PARTIAL: (
                NodeExecutionStatus.SUCCEEDED,
                PlanExecutionStatus.PARTIAL,
                ResultStatus.PARTIAL,
            ),
            ExplorationStatus.FAILED: (
                NodeExecutionStatus.FAILED,
                PlanExecutionStatus.FAILED,
                ResultStatus.FAILED,
            ),
            ExplorationStatus.DENIED: (
                NodeExecutionStatus.DENIED,
                PlanExecutionStatus.DENIED,
                ResultStatus.DENIED,
            ),
            ExplorationStatus.CANCELLED: (
                NodeExecutionStatus.CANCELLED,
                PlanExecutionStatus.CANCELLED,
                ResultStatus.CANCELLED,
            ),
        }
        expected = terminal.get(exploration.status)
        if expected is None:
            raise ValueError("unsupported exploration status")
        outer_status, plan_status, result_status = expected
        if outer.status is not outer_status or self.state.status is not plan_status:
            raise ValueError("terminal exploration status must match outer node and plan")
        if exploration.final_result is None or outer.result != exploration.final_result:
            raise ValueError("terminal exploration result must equal outer node result")
        if exploration.final_result.status is not result_status:
            raise ValueError("terminal exploration result status is inconsistent")
        if (
            exploration.completed_at is None
            or outer.completed_at != exploration.completed_at
            or self.state.completed_at != exploration.completed_at
        ):
            raise ValueError("terminal exploration completed_at must be atomic")

    @property
    def state_version(self) -> int:
        """返回用于存储索引和未来乐观并发控制的状态版本。"""

        return self.state.state_version
