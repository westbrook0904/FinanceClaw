"""业务状态机无关的 StateStore SPI。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import PlanExecutionRecord


class StateStore(ABC):
    """保存和读取完整 Plan 快照，不解释节点状态迁移。"""

    @abstractmethod
    async def create(self, record: PlanExecutionRecord) -> None:
        """原子创建一条记录；plan_id 已存在时失败。"""

    @abstractmethod
    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        """加载记录快照；不存在时返回 ``None``。"""

    @abstractmethod
    async def save(self, record: PlanExecutionRecord) -> None:
        """原子替换已存在的完整快照。"""

    @abstractmethod
    async def delete(self, plan_id: str) -> None:
        """删除记录；不存在时保持幂等。"""


def validate_record(record: PlanExecutionRecord) -> None:
    if not isinstance(record, PlanExecutionRecord):
        raise TypeError("record must be PlanExecutionRecord")


def validate_plan_id(plan_id: str) -> None:
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise TypeError("plan_id must be a non-empty string")
