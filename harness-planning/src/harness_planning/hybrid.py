"""仅在 primary 明确 NOT_APPLICABLE 时切换策略的 HybridPlanner。"""

from __future__ import annotations

from harness_contracts import ExecutionPlan, PlannerNotApplicableError

from .context import PlanningContext
from .models import PlanningAttemptObserver
from .planner import Planner, validate_planner_id, validate_planner_output
from .validator import PlanValidator


class HybridPlanner(Planner):
    """确定性优先；非法、安全拒绝或超时均禁止静默 fallback。"""

    def __init__(
        self,
        planner_id: str,
        primary: Planner,
        fallback: Planner,
        *,
        validator: PlanValidator | None = None,
    ) -> None:
        self._planner_id = validate_planner_id(planner_id)
        if not isinstance(primary, Planner):
            raise TypeError("primary must implement Planner")
        if not isinstance(fallback, Planner):
            raise TypeError("fallback must implement Planner")
        if validator is not None and not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")

        self._primary = primary
        self._fallback = fallback
        self._validator = validator or PlanValidator()

    @property
    def planner_id(self) -> str:
        return self._planner_id

    @property
    def primary(self) -> Planner:
        return self._primary

    @property
    def fallback(self) -> Planner:
        return self._fallback

    @property
    def validator(self) -> PlanValidator:
        return self._validator

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        return await self.plan_with_observer(context)

    async def plan_with_observer(
        self,
        context: PlanningContext,
        *,
        attempt_observer: PlanningAttemptObserver | None = None,
    ) -> ExecutionPlan:
        if not isinstance(context, PlanningContext):
            raise TypeError("context must be PlanningContext")
        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")

        selected = self._primary
        try:
            output = await selected.plan_with_observer(
                context,
                attempt_observer=attempt_observer,
            )
        except PlannerNotApplicableError:
            selected = self._fallback
            output = await selected.plan_with_observer(
                context,
                attempt_observer=attempt_observer,
            )

        return validate_planner_output(
            output,
            self._validator,
            planner_id=selected.planner_id,
        )
