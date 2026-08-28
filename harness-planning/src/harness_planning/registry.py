"""Composition Root 构造期冻结的本地 Planner 注册表。"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from harness_contracts import ErrorCode, PlanningError

from .planner import Planner, validate_planner_id


class PlannerRegistry:
    """按稳定 ID 查询 Planner；构造完成后不支持动态注册。"""

    def __init__(self, planners: Iterable[Planner] = ()) -> None:
        if isinstance(planners, str):
            raise TypeError("planners must be an iterable of Planner values")

        index: dict[str, Planner] = {}
        for planner in planners:
            if not isinstance(planner, Planner):
                raise TypeError("planners must contain Planner values")
            planner_id = validate_planner_id(planner.planner_id)
            if planner_id in index:
                raise ValueError(f"duplicate planner_id: {planner_id}")
            index[planner_id] = planner

        self._planners = MappingProxyType(index)
        self._planner_ids = tuple(index)

    @property
    def planner_ids(self) -> tuple[str, ...]:
        return self._planner_ids

    def list(self) -> tuple[str, ...]:
        """按构造顺序返回不可变 Planner ID 快照。"""

        return self._planner_ids

    def get(self, planner_id: str) -> Planner:
        """返回已配置 Planner；未知 ID 使用稳定 PlanningError fail-closed。"""

        canonical_id = validate_planner_id(planner_id)
        planner = self._planners.get(canonical_id)
        if planner is None:
            raise PlanningError(
                "planner is not configured",
                code=ErrorCode.PLANNER_NOT_CONFIGURED,
                details={"planner_id": canonical_id},
            )
        return planner

    def __len__(self) -> int:
        return len(self._planner_ids)
