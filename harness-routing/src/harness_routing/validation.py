"""Router 输出进入 Harness dispatch 前的独立校验边界。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_contracts import (
    CapabilityType,
    ErrorCode,
    ExecutionMode,
    PlanningError,
    RouteDecision,
    RoutingError,
)
from pydantic import ValidationError

from .models import RoutingContext

_STAGE3B_AVAILABLE_MODES = frozenset({ExecutionMode.FAST, ExecutionMode.PLAN})


class RouteDecisionValidator:
    """不信任 Router 实现，重新校验 schema、请求、Catalog 与 Policy 约束。"""

    def __init__(self, *, planner_ids: Iterable[str] = ()) -> None:
        if isinstance(planner_ids, str):
            raise TypeError("planner_ids must be an iterable of strings")
        known_planner_ids = frozenset(planner_ids)
        if any(
            not isinstance(planner_id, str) or not planner_id.strip()
            for planner_id in known_planner_ids
        ):
            raise TypeError("planner_ids must contain non-empty strings")
        self._planner_ids = frozenset(item.strip() for item in known_planner_ids)

    @property
    def planner_ids(self) -> frozenset[str]:
        return self._planner_ids

    def validate(
        self,
        decision: RouteDecision,
        context: RoutingContext,
    ) -> RouteDecision:
        if not isinstance(context, RoutingContext):
            raise TypeError("context must be RoutingContext")
        if not isinstance(decision, RouteDecision):
            self._raise_invalid("router must return RouteDecision")

        try:
            RouteDecision.model_validate(decision.model_dump(mode="json"))
        except (ValidationError, TypeError, ValueError) as exc:
            self._raise_invalid(
                "router returned a structurally invalid decision",
                reason=type(exc).__name__,
            )

        requested_mode = context.requested_mode
        if requested_mode is not ExecutionMode.AUTO and decision.mode is not requested_mode:
            self._raise_invalid(
                "router changed a fixed request mode",
                requested_mode=requested_mode.value,
                decision_mode=decision.mode.value,
            )

        constraints = context.constraints
        if constraints.forced_mode is not None and decision.mode is not constraints.forced_mode:
            raise RoutingError(
                "route decision violates forced mode",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={
                    "forced_mode": constraints.forced_mode.value,
                    "decision_mode": decision.mode.value,
                },
            )
        if constraints.allowed_modes is not None and decision.mode not in constraints.allowed_modes:
            raise RoutingError(
                "route mode is not allowed by policy",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={"decision_mode": decision.mode.value},
            )

        if decision.mode not in _STAGE3B_AVAILABLE_MODES:
            raise RoutingError(
                "route mode is not available in Stage 3B",
                code=ErrorCode.ROUTE_MODE_NOT_AVAILABLE,
                details={"decision_mode": decision.mode.value},
            )

        if decision.mode is ExecutionMode.FAST:
            capability_id = decision.capability_id
            catalog = {descriptor.id: descriptor for descriptor in context.catalog_snapshot}
            descriptor = catalog.get(capability_id)
            if descriptor is None:
                self._raise_invalid(
                    "route capability does not exist in the catalog snapshot",
                    capability_id=capability_id,
                )
            if descriptor.type not in {CapabilityType.AGENT, CapabilityType.TOOL}:
                self._raise_invalid(
                    "route capability is not directly executable",
                    capability_id=capability_id,
                )
            if (
                constraints.allowed_capability_ids is not None
                and capability_id not in constraints.allowed_capability_ids
            ):
                raise RoutingError(
                    "route capability is not allowed by policy",
                    code=ErrorCode.ROUTE_CAPABILITY_NOT_ALLOWED,
                    details={"capability_id": capability_id},
                )

            request_target = context.invocation.request.target
            if request_target is not None and capability_id != request_target.capability:
                self._raise_invalid(
                    "router changed the request target capability",
                    request_capability_id=request_target.capability,
                    decision_capability_id=capability_id,
                )

        if decision.mode is ExecutionMode.PLAN:
            planner_id = decision.planner_id
            if planner_id not in self._planner_ids:
                raise PlanningError(
                    "route planner is not configured",
                    code=ErrorCode.PLANNER_NOT_CONFIGURED,
                    details={"planner_id": planner_id},
                )
            if (
                constraints.allowed_planner_ids is not None
                and planner_id not in constraints.allowed_planner_ids
            ):
                raise RoutingError(
                    "route planner is not allowed by policy",
                    code=ErrorCode.ROUTE_PLANNER_NOT_ALLOWED,
                    details={"planner_id": planner_id},
                )

        return decision

    @staticmethod
    def _raise_invalid(message: str, **details: str | None) -> None:
        raise RoutingError(
            message,
            code=ErrorCode.ROUTE_INVALID_DECISION,
            details={key: value for key, value in details.items() if value is not None},
        )
