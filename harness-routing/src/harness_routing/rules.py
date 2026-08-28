"""无需模型的确定性 RuleRouter。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Self

from harness_contracts import (
    ContractModel,
    ErrorCode,
    ExecutionMode,
    PlanningError,
    RouteDecision,
    RouteSource,
    RouteType,
    RoutingError,
)
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from pydantic import Field, model_validator

from .models import RoutingContext
from .router import Router

_STAGE3B_UNAVAILABLE_COMPONENT_ID = "stage3b-unavailable"


class InputTypeRouteRule(ContractModel):
    """把一个稳定 input type 映射到 FAST Capability 或 PLAN Planner。"""

    input_type: NonEmptyString
    mode: ExecutionMode
    capability_id: NonEmptyString | None = None
    planner_id: NonEmptyString | None = None
    reason_code: NonEmptyString = "INPUT_TYPE_RULE"
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_rule_shape(self) -> Self:
        if self.mode is ExecutionMode.FAST:
            if self.capability_id is None or self.planner_id is not None:
                raise ValueError("FAST input type rule requires only capability_id")
            return self
        if self.mode is ExecutionMode.PLAN:
            if self.planner_id is None or self.capability_id is not None:
                raise ValueError("PLAN input type rule requires only planner_id")
            return self
        raise ValueError("input type rule mode must be FAST or PLAN")

    def to_decision(self) -> RouteDecision:
        if self.mode is ExecutionMode.FAST:
            return RouteDecision(
                mode=self.mode,
                route_type=RouteType.DIRECT_CAPABILITY,
                source=RouteSource.RULE,
                capability_id=self.capability_id,
                confidence=1.0,
                reason_code=self.reason_code,
                metadata=self.metadata,
            )
        return RouteDecision(
            mode=self.mode,
            route_type=RouteType.GENERATED_PLAN,
            source=RouteSource.RULE,
            planner_id=self.planner_id,
            confidence=1.0,
            reason_code=self.reason_code,
            metadata=self.metadata,
        )


class RuleRouter(Router):
    """按固定优先级匹配显式模式、target 和 input-type 规则。"""

    def __init__(
        self,
        *,
        router_id: str = "rule-router",
        default_planner_id: str | None = None,
        input_type_rules: Iterable[InputTypeRouteRule] = (),
        fallback: Router | None = None,
    ) -> None:
        if not isinstance(router_id, str) or not router_id.strip():
            raise TypeError("router_id must be a non-empty string")
        if default_planner_id is not None and (
            not isinstance(default_planner_id, str) or not default_planner_id.strip()
        ):
            raise TypeError("default_planner_id must be a non-empty string when provided")
        if fallback is not None and not isinstance(fallback, Router):
            raise TypeError("fallback must implement Router")

        rules = tuple(input_type_rules)
        if any(not isinstance(rule, InputTypeRouteRule) for rule in rules):
            raise TypeError("input_type_rules must contain InputTypeRouteRule values")
        input_types = [rule.input_type for rule in rules]
        if len(input_types) != len(set(input_types)):
            raise ValueError("input_type_rules must not contain duplicate input types")

        self._router_id = router_id.strip()
        self._default_planner_id = (
            default_planner_id.strip() if default_planner_id is not None else None
        )
        self._rules = {rule.input_type: rule for rule in rules}
        self._fallback = fallback

    @property
    def router_id(self) -> str:
        return self._router_id

    async def route(self, context: RoutingContext) -> RouteDecision:
        if not isinstance(context, RoutingContext):
            raise TypeError("context must be RoutingContext")

        requested_mode = context.requested_mode
        if requested_mode is ExecutionMode.EXPLORE:
            return RouteDecision(
                mode=ExecutionMode.EXPLORE,
                route_type=RouteType.EXPLORATION,
                source=RouteSource.REQUEST,
                explorer_id=_STAGE3B_UNAVAILABLE_COMPONENT_ID,
                confidence=1.0,
                reason_code="REQUEST_MODE_EXPLORE",
            )
        if requested_mode is ExecutionMode.HYBRID:
            return RouteDecision(
                mode=ExecutionMode.HYBRID,
                route_type=RouteType.HYBRID,
                source=RouteSource.REQUEST,
                planner_id=self._default_planner_id or _STAGE3B_UNAVAILABLE_COMPONENT_ID,
                explorer_id=_STAGE3B_UNAVAILABLE_COMPONENT_ID,
                confidence=1.0,
                reason_code="REQUEST_MODE_HYBRID",
            )

        target = context.request_summary.target_capability
        if target is not None and requested_mode in {ExecutionMode.AUTO, ExecutionMode.FAST}:
            return RouteDecision(
                mode=ExecutionMode.FAST,
                route_type=RouteType.DIRECT_CAPABILITY,
                source=RouteSource.REQUEST,
                capability_id=target,
                confidence=1.0,
                reason_code="EXPLICIT_TARGET",
            )

        if requested_mode is ExecutionMode.PLAN:
            if self._default_planner_id is None:
                raise PlanningError(
                    "PLAN mode requires a configured default planner",
                    code=ErrorCode.PLANNER_NOT_CONFIGURED,
                    details={"router_id": self.router_id},
                )
            return RouteDecision(
                mode=ExecutionMode.PLAN,
                route_type=RouteType.GENERATED_PLAN,
                source=RouteSource.REQUEST,
                planner_id=self._default_planner_id,
                confidence=1.0,
                reason_code="REQUEST_MODE_PLAN",
            )

        rule = self._rules.get(context.request_summary.input_type)
        if rule is not None and (
            requested_mode is ExecutionMode.AUTO or rule.mode is ExecutionMode.FAST
        ):
            return rule.to_decision()

        if self._fallback is not None:
            return await self._fallback.route(context)

        raise RoutingError(
            "no deterministic routing rule matched the request",
            code=ErrorCode.ROUTE_NO_MATCH,
            details={
                "router_id": self.router_id,
                "requested_mode": requested_mode.value,
                "input_type": context.request_summary.input_type,
            },
        )
