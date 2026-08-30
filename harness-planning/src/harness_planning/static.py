"""按确定性 request route key 选择模板或 factory 的 StaticPlanner。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType

from harness_contracts import (
    ErrorCode,
    ExecutionPlan,
    HarnessError,
    PlannerNotApplicableError,
    PlanningError,
)

from .context import PlanningContext
from .identity import (
    PlanIdentityFactory,
    PlanMaterializer,
    PlannerArtifact,
    PlannerOutputNormalizer,
    PlanTemplate,
)
from .models import PlanningAttemptObserver, PlanValidationError
from .planner import Planner, validate_planner_id, validate_planner_output
from .validator import PlanValidator

type PlanFactory = Callable[[PlanningContext], PlannerArtifact | Awaitable[PlannerArtifact]]
type StaticPlanRoute = PlannerArtifact | PlanFactory
type RouteKeyFactory = Callable[[PlanningContext], str]


class StaticPlanner(Planner):
    """用显式 route-key 映射生成无需模型的确定性 ExecutionPlan。"""

    def __init__(
        self,
        planner_id: str,
        routes: Mapping[str, StaticPlanRoute],
        *,
        validator: PlanValidator | None = None,
        route_key: RouteKeyFactory | None = None,
        plan_identity_factory: PlanIdentityFactory | None = None,
    ) -> None:
        self._planner_id = validate_planner_id(planner_id)
        if not isinstance(routes, Mapping):
            raise TypeError("routes must be a mapping")

        copied_routes: dict[str, StaticPlanRoute] = {}
        for key, template in routes.items():
            if not isinstance(key, str) or not key.strip():
                raise TypeError("route keys must be non-empty strings")
            if key != key.strip():
                raise ValueError("route keys must not include surrounding whitespace")
            if not isinstance(template, PlanTemplate | ExecutionPlan) and not callable(template):
                raise TypeError(
                    "route values must be PlanTemplate/ExecutionPlan values or callables"
                )
            copied_routes[key] = template

        if validator is not None and not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")
        if route_key is not None and not callable(route_key):
            raise TypeError("route_key must be callable")
        if plan_identity_factory is not None and not isinstance(
            plan_identity_factory,
            PlanIdentityFactory,
        ):
            raise TypeError("plan_identity_factory must be PlanIdentityFactory")

        self._routes = MappingProxyType(copied_routes)
        self._validator = validator or PlanValidator()
        self._route_key = route_key or (lambda context: context.goal.input_type)
        self._normalizer = PlannerOutputNormalizer()
        self._materializer = PlanMaterializer(plan_identity_factory)

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
        template = await self.plan_artifact(context)
        plan = self._materializer.materialize(
            template,
            context.invocation,
            planner_id=self.planner_id,
        )
        return validate_planner_output(
            plan,
            self._validator,
            planner_id=self.planner_id,
        )

    async def plan_artifact(self, context: PlanningContext) -> PlanTemplate:
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
        normalized = self._normalizer.normalize(
            output,
            planner_id=self.planner_id,
        )
        try:
            return self._validator.validate_template(normalized)
        except PlanValidationError as exc:
            codes = sorted({issue.code.value for issue in exc.issues})
            raise PlanningError(
                "planner returned an invalid plan template",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "validation_codes": codes,
                },
            ) from exc
        except Exception as exc:
            raise PlanningError(
                "planner output validation failed",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc

    async def plan_artifact_with_observer(
        self,
        context: PlanningContext,
        *,
        attempt_observer: PlanningAttemptObserver | None = None,
    ) -> PlanTemplate:
        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")
        return await self.plan_artifact(context)
