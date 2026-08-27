"""请求执行模式与路由决策的稳定协议。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from .base import ContractModel, FrozenJsonMapping, NonEmptyString


class ExecutionMode(StrEnum):
    """调用方要求或 Harness 最终选择的执行模式。"""

    AUTO = "auto"
    FAST = "fast"
    PLAN = "plan"
    EXPLORE = "explore"
    HYBRID = "hybrid"


class RouteType(StrEnum):
    """Harness 根据路由决策分派的执行路径。"""

    DIRECT_CAPABILITY = "direct_capability"
    GENERATED_PLAN = "generated_plan"
    EXPLORATION = "exploration"
    HYBRID = "hybrid"


class RouteSource(StrEnum):
    """路由决策的来源，仅用于解释决策，不授予执行权限。"""

    REQUEST = "request"
    POLICY = "policy"
    RULE = "rule"
    MODEL = "model"


class RouteDecision(ContractModel):
    """Router 提交给 Harness 校验和分派的结构化决策。"""

    mode: ExecutionMode
    route_type: RouteType
    source: RouteSource
    capability_id: NonEmptyString | None = None
    planner_id: NonEmptyString | None = None
    explorer_id: NonEmptyString | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_code: NonEmptyString
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_route_shape(self) -> Self:
        """保证 mode、route_type 和路由目标形成唯一合法组合。"""

        if self.mode is ExecutionMode.AUTO:
            raise ValueError("AUTO cannot appear in a final RouteDecision")

        expected_route_type = {
            ExecutionMode.FAST: RouteType.DIRECT_CAPABILITY,
            ExecutionMode.PLAN: RouteType.GENERATED_PLAN,
            ExecutionMode.EXPLORE: RouteType.EXPLORATION,
            ExecutionMode.HYBRID: RouteType.HYBRID,
        }[self.mode]
        if self.route_type is not expected_route_type:
            raise ValueError(
                f"{self.mode.value} mode requires {expected_route_type.value} route_type"
            )

        required_fields = {
            ExecutionMode.FAST: ("capability_id",),
            ExecutionMode.PLAN: ("planner_id",),
            ExecutionMode.EXPLORE: ("explorer_id",),
            ExecutionMode.HYBRID: ("planner_id", "explorer_id"),
        }[self.mode]
        target_fields = ("capability_id", "planner_id", "explorer_id")
        missing_fields = [
            field_name for field_name in required_fields if getattr(self, field_name) is None
        ]
        if missing_fields:
            raise ValueError(f"missing required route fields: {', '.join(missing_fields)}")

        forbidden_fields = [
            field_name
            for field_name in target_fields
            if field_name not in required_fields and getattr(self, field_name) is not None
        ]
        if forbidden_fields:
            raise ValueError(f"forbidden route fields: {', '.join(forbidden_fields)}")

        return self
