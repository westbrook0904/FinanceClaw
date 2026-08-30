"""仅在 primary 明确 NOT_APPLICABLE 时切换策略的 HybridPlanner。"""

from __future__ import annotations

from harness_contracts import ErrorCode, ExecutionPlan, PlannerNotApplicableError, PlanningError

from .context import PlanningContext
from .identity import PlanMaterializer, PlannerOutputNormalizer, PlanTemplate
from .models import PlanningAttemptObserver, PlanValidationError
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
        self._normalizer = PlannerOutputNormalizer()
        self._materializer = PlanMaterializer()

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
        template = await self.plan_artifact_with_observer(
            context,
            attempt_observer=attempt_observer,
        )
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
        return await self.plan_artifact_with_observer(context)

    async def plan_artifact_with_observer(
        self,
        context: PlanningContext,
        *,
        attempt_observer: PlanningAttemptObserver | None = None,
    ) -> PlanTemplate:
        if not isinstance(context, PlanningContext):
            raise TypeError("context must be PlanningContext")
        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")

        selected = self._primary
        try:
            output = await selected.plan_artifact_with_observer(
                context,
                attempt_observer=attempt_observer,
            )
        except PlannerNotApplicableError:
            selected = self._fallback
            output = await selected.plan_artifact_with_observer(
                context,
                attempt_observer=attempt_observer,
            )

        template = self._normalizer.normalize(
            output,
            planner_id=selected.planner_id,
        )
        try:
            return self._validator.validate_template(template)
        except PlanValidationError as exc:
            raise PlanningError(
                "planner returned an invalid plan template",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": selected.planner_id,
                    "validation_codes": sorted({issue.code.value for issue in exc.issues}),
                },
            ) from exc
