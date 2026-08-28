"""按确定性 request route key 选择模板或 factory 的 StaticPlanner。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType

from harness_contracts import ExecutionPlan, HarnessError, PlannerNotApplicableError, PlanningError

from .context import PlanningContext
from .planner import Planner, validate_planner_id, validate_planner_output
from .validator import PlanValidator

type PlanFactory = Callable[[PlanningContext], ExecutionPlan | Awaitable[ExecutionPlan]]
type PlanTemplate = ExecutionPlan | PlanFactory
type RouteKeyFactory = Callable[[PlanningContext], str]


class StaticPlanner(Planner):
    """用显式 route-key 映射生成无需模型的确定性 ExecutionPlan。"""

    def __init__(
        self,
        planner_id: str,
        routes: Mapping[str, PlanTemplate],
        *,
        validator: PlanValidator | None = None,
        route_key: RouteKeyFactory | None = None,
    ) -> None:
        self._planner_id = validate_planner_id(planner_id)
        if not isinstance(routes, Mapping):
            raise TypeError("routes must be a mapping")

        copied_routes: dict[str, PlanTemplate] = {}
        for key, template in routes.items():
            if not isinstance(key, str) or not key.strip():
                raise TypeError("route keys must be non-empty strings")
            if key != key.strip():
                raise ValueError("route keys must not include surrounding whitespace")
            if not isinstance(template, ExecutionPlan) and not callable(template):
                raise TypeError("route values must be ExecutionPlan values or callables")
            copied_routes[key] = template

        if validator is not None and not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")
        if route_key is not None and not callable(route_key):
            raise TypeError("route_key must be callable")

        self._routes = MappingProxyType(copied_routes)
        self._validator = validator or PlanValidator()
        self._route_key = route_key or (lambda context: context.goal.input_type)

    @property
    def planner_id(self) -> str:
        return self._planner_id

    @property
    def route_keys(self) -> tuple[str, ...]:
        return tuple(self._routes)

    @property
    def validator(self) -> PlanValidator:
        return self._validator

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        if not isinstance(context, PlanningContext):
            raise TypeError("context must be PlanningContext")

        try:
            key = self._route_key(context)
        except HarnessError:
            raise
        except Exception as exc:
            raise PlanningError(
                "static planner route key evaluation failed",
                details={
                    "planner_id": self.planner_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        if not isinstance(key, str) or not key.strip():
            raise PlanningError(
                "static planner route key must be a non-empty string",
                details={"planner_id": self.planner_id},
            )

        template = self._routes.get(key)
        if template is None:
            raise PlannerNotApplicableError(
                "static planner has no route for request",
                details={"planner_id": self.planner_id, "route_key": key},
            )

        try:
            output = template(context) if callable(template) else template
            if inspect.isawaitable(output):
                output = await output
        except HarnessError:
            raise
        except Exception as exc:
            raise PlanningError(
                "static plan factory failed",
                details={
                    "planner_id": self.planner_id,
                    "route_key": key,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        return validate_planner_output(
            output,
            self._validator,
            planner_id=self.planner_id,
        )
