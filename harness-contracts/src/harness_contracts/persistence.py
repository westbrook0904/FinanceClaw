"""ExecutionPlan 持久化与恢复使用的稳定快照协议。"""

from __future__ import annotations

from pydantic import model_validator

from .base import ContractModel, NonEmptyString
from .context import InvocationContext
from .execution import PlanExecutionState
from .plan import ExecutionPlan


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
        return self

    @property
    def state_version(self) -> int:
        """返回用于存储索引和未来乐观并发控制的状态版本。"""

        return self.state.state_version
