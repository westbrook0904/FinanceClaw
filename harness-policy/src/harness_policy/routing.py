"""PRE_ROUTE Policy 约束的安全合并与门禁结果。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from harness_contracts import (
    ContractModel,
    ErrorCode,
    ExecutionMode,
    PolicyError,
    RoutingError,
)
from harness_contracts.base import JsonValue
from harness_routing import RoutePolicyConstraints
from pydantic import ValidationError

from .models import PolicyDecision, PolicyEffect


class PreRoutePolicyResult(ContractModel):
    """通过 PRE_ROUTE 门禁后交给 Router 的有效模式与类型化约束。"""

    decision: PolicyDecision
    effective_mode: ExecutionMode
    constraints: RoutePolicyConstraints


class RoutePolicyConstraintReducer:
    """按安全收紧语义合并多个 PRE_ROUTE Policy constraint payload。"""

    def __init__(self) -> None:
        self._constraints = RoutePolicyConstraints()

    @property
    def constraints(self) -> RoutePolicyConstraints:
        return self._constraints

    def add(
        self,
        payload: Mapping[str, JsonValue] | RoutePolicyConstraints,
    ) -> RoutePolicyConstraints:
        incoming = _parse_constraints(payload)
        current = self._constraints

        forced_mode = current.forced_mode
        if incoming.forced_mode is not None:
            if forced_mode is not None and forced_mode is not incoming.forced_mode:
                raise RoutingError(
                    "pre-route policies require conflicting execution modes",
                    code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                    details={
                        "current_forced_mode": forced_mode.value,
                        "incoming_forced_mode": incoming.forced_mode.value,
                    },
                )
            forced_mode = incoming.forced_mode

        allowed_modes = _intersect(current.allowed_modes, incoming.allowed_modes)
        if allowed_modes is not None and not allowed_modes:
            raise RoutingError(
                "pre-route allowed mode intersection is empty",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={"reason": "allowed_modes_empty_intersection"},
            )
        if (
            forced_mode is not None
            and allowed_modes is not None
            and forced_mode not in allowed_modes
        ):
            raise RoutingError(
                "forced route mode is excluded by allowed modes",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={"forced_mode": forced_mode.value},
            )

        self._constraints = RoutePolicyConstraints(
            forced_mode=forced_mode,
            allowed_modes=allowed_modes,
            allowed_capability_ids=_intersect(
                current.allowed_capability_ids,
                incoming.allowed_capability_ids,
            ),
            allowed_planner_ids=_intersect(
                current.allowed_planner_ids,
                incoming.allowed_planner_ids,
            ),
            max_plan_attempts=_minimum(
                current.max_plan_attempts,
                incoming.max_plan_attempts,
            ),
            max_plan_nodes=_minimum(
                current.max_plan_nodes,
                incoming.max_plan_nodes,
            ),
        )
        return self._constraints


def reduce_route_policy_constraints(
    payloads: Iterable[Mapping[str, JsonValue] | RoutePolicyConstraints],
) -> RoutePolicyConstraints:
    """一次性安全合并一组 PRE_ROUTE constraint payload。"""

    reducer = RoutePolicyConstraintReducer()
    for payload in payloads:
        reducer.add(payload)
    return reducer.constraints


def resolve_pre_route_policy(
    decision: PolicyDecision,
    requested_mode: ExecutionMode,
) -> PreRoutePolicyResult:
    """执行 PRE_ROUTE effect 门禁并派生 Router 应看到的有效请求模式。"""

    if not isinstance(decision, PolicyDecision):
        raise TypeError("decision must be PolicyDecision")
    if not isinstance(requested_mode, ExecutionMode):
        raise TypeError("requested_mode must be ExecutionMode")

    serialized = decision.model_dump(mode="json")["constraints"]
    if decision.effect is PolicyEffect.DENY:
        raise PolicyError(
            decision.reason or "pre-route policy denied request",
            details={"policy": decision.policy},
        )
    if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
        raise RoutingError(
            "pre-route approval is not supported in Stage 3B",
            code=ErrorCode.ROUTE_APPROVAL_NOT_SUPPORTED,
            details={"policy": decision.policy},
        )

    constraints = _parse_constraints(serialized)
    effective_mode = requested_mode
    if constraints.forced_mode is not None:
        if (
            requested_mode is not ExecutionMode.AUTO
            and requested_mode is not constraints.forced_mode
        ):
            raise RoutingError(
                "pre-route policy cannot change an explicit execution mode",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={
                    "requested_mode": requested_mode.value,
                    "forced_mode": constraints.forced_mode.value,
                },
            )
        effective_mode = constraints.forced_mode

    if (
        effective_mode is not ExecutionMode.AUTO
        and constraints.allowed_modes is not None
        and effective_mode not in constraints.allowed_modes
    ):
        raise RoutingError(
            "requested execution mode is not allowed by pre-route policy",
            code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
            details={"requested_mode": effective_mode.value},
        )

    return PreRoutePolicyResult(
        decision=decision,
        effective_mode=effective_mode,
        constraints=constraints,
    )


def _parse_constraints(
    payload: Mapping[str, JsonValue] | RoutePolicyConstraints,
) -> RoutePolicyConstraints:
    if isinstance(payload, RoutePolicyConstraints):
        return payload
    if not isinstance(payload, Mapping):
        raise PolicyError(
            "pre-route policy returned invalid constraints",
            details={
                "reason": "invalid_route_constraints",
                "issue_count": 1,
            },
        )
    try:
        return RoutePolicyConstraints.model_validate(dict(payload))
    except ValidationError as exc:
        raise PolicyError(
            "pre-route policy returned invalid constraints",
            details={
                "reason": "invalid_route_constraints",
                "issue_count": exc.error_count(),
            },
        ) from exc


def _intersect[T](
    current: frozenset[T] | None,
    incoming: frozenset[T] | None,
) -> frozenset[T] | None:
    if current is None:
        return incoming
    if incoming is None:
        return current
    return current & incoming


def _minimum(current: int | None, incoming: int | None) -> int | None:
    if current is None:
        return incoming
    if incoming is None:
        return current
    return min(current, incoming)
