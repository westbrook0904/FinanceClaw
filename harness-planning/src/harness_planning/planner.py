"""Planner SPI 与不可信 Planner 输出的统一验证。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import ErrorCode, ExecutionPlan, PlanningError

from .context import PlanningContext
from .identity import PlannerArtifact
from .models import PlanningAttemptObserver, PlanValidationError
from .validator import PlanValidator


class Planner(ABC):
    """只生成标准 ExecutionPlan、不执行计划或业务能力的规划接口。"""

    @property
    @abstractmethod
    def planner_id(self) -> str:
        """返回用于注册、配置和可观测性的稳定 Planner ID。"""

    @abstractmethod
    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        """根据受限 PlanningContext 生成一个经过验证的 ExecutionPlan。"""

    async def plan_artifact(self, context: PlanningContext) -> PlannerArtifact:
        """返回 identity-free 模板；legacy Planner 默认返回 ExecutionPlan candidate。"""

        return await self.plan(context)

    async def plan_with_observer(
        self,
        context: PlanningContext,
        *,
        attempt_observer: PlanningAttemptObserver | None = None,
    ) -> ExecutionPlan:
        """为单次调用附加安全 attempt observer；非生成式 Planner 默认忽略。"""

        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")
        return await self.plan(context)

    async def plan_artifact_with_observer(
        self,
        context: PlanningContext,
        *,
        attempt_observer: PlanningAttemptObserver | None = None,
    ) -> PlannerArtifact:
        """Coordinator-facing artifact API，兼容旧的 observer-aware Planner。"""

        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")
        # 新 Planner 显式覆写 plan_artifact() 时必须优先使用无身份 artifact；旧 Planner
        # 若只覆写 plan_with_observer()，则保留其 attempt 观察兼容行为。
        if type(self).plan_artifact is not Planner.plan_artifact:
            return await self.plan_artifact(context)
        return await self.plan_with_observer(
            context,
            attempt_observer=attempt_observer,
        )


def validate_planner_id(planner_id: str) -> str:
    """校验并返回 canonical Planner ID。"""

    if not isinstance(planner_id, str) or not planner_id.strip():
        raise TypeError("planner_id must be a non-empty string")
    if planner_id != planner_id.strip():
        raise ValueError("planner_id must not include surrounding whitespace")
    return planner_id


def validate_planner_output(
    output: object,
    validator: PlanValidator,
    *,
    planner_id: str,
) -> ExecutionPlan:
    """把 delegate 输出收敛为稳定 PlanningError，不泄露完整 Plan 内容。"""

    if not isinstance(output, ExecutionPlan):
        raise PlanningError(
            "planner returned a non-ExecutionPlan output",
            code=ErrorCode.PLANNER_INVALID_OUTPUT,
            details={
                "planner_id": planner_id,
                "output_type": type(output).__name__,
            },
        )
    try:
        return validator.validate(output)
    except PlanValidationError as exc:
        validation_codes = sorted({issue.code.value for issue in exc.issues})
        raise PlanningError(
            "planner returned an invalid execution plan",
            code=ErrorCode.PLANNER_INVALID_OUTPUT,
            details={
                "planner_id": planner_id,
                "issue_count": len(exc.issues),
                "validation_codes": validation_codes,
            },
        ) from exc
    except Exception as exc:
        raise PlanningError(
            "planner output validation failed",
            code=ErrorCode.PLANNER_INVALID_OUTPUT,
            details={
                "planner_id": planner_id,
                "cause_type": type(exc).__name__,
            },
        ) from exc
