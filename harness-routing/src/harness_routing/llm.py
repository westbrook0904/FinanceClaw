"""通过 ModelGateway 生成结构化提议、但不获得执行权的 LLMRouter。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from harness_contracts import (
    CapabilityType,
    ErrorCode,
    ExecutionMode,
    RouteDecision,
    RouteSource,
    RoutingError,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelGateway,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
)
from pydantic import ValidationError

from .models import RoutingContext
from .router import Router
from .validation import RouteDecisionValidator

_SYSTEM_PROMPT = """You are a routing decision component inside a controlled Harness.
Return exactly one JSON object matching the supplied RouteDecision schema.
Choose only from the supplied execution modes, capability IDs, and planner IDs.
Never output provider IDs, plugin IDs, credentials, or executable instructions.
Do not call tools, execute capabilities, or invent unavailable targets."""

_STAGE3B_ROUTABLE_MODES = frozenset({ExecutionMode.FAST, ExecutionMode.PLAN})


class LLMRouter(Router):
    """使用逻辑 Model Capability 生成并验证 RouteDecision。"""

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        route_model_capability_id: str,
        decision_validator: RouteDecisionValidator,
        router_id: str = "llm-router",
        prompt_version: str = "route-v1",
        max_output_tokens: int | None = None,
    ) -> None:
        if not isinstance(model_gateway, ModelGateway):
            raise TypeError("model_gateway must be ModelGateway")
        for field_name, value in (
            ("route_model_capability_id", route_model_capability_id),
            ("router_id", router_id),
            ("prompt_version", prompt_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise TypeError(f"{field_name} must be a non-empty string")
            if value != value.strip():
                raise ValueError(f"{field_name} must not include surrounding whitespace")
        if not isinstance(decision_validator, RouteDecisionValidator):
            raise TypeError("decision_validator must be RouteDecisionValidator")
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise TypeError("max_output_tokens must be a positive integer when provided")

        self._model_gateway = model_gateway
        self._route_model_capability_id = route_model_capability_id
        self._decision_validator = decision_validator
        self._router_id = router_id
        self._prompt_version = prompt_version
        self._max_output_tokens = max_output_tokens
        self._response_schema = RouteDecision.model_json_schema()

    @property
    def router_id(self) -> str:
        return self._router_id

    @property
    def model_gateway(self) -> ModelGateway:
        return self._model_gateway

    @property
    def route_model_capability_id(self) -> str:
        return self._route_model_capability_id

    @property
    def decision_validator(self) -> RouteDecisionValidator:
        return self._decision_validator

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    async def route(self, context: RoutingContext) -> RouteDecision:
        if not isinstance(context, RoutingContext):
            raise TypeError("context must be RoutingContext")

        request = GenerateRequest(
            model=self._route_model_capability_id,
            messages=(
                ModelMessage(role=ModelRole.SYSTEM, content=_SYSTEM_PROMPT),
                ModelMessage(
                    role=ModelRole.USER,
                    content=json.dumps(
                        self._prompt_payload(context),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            ),
            response_format=ModelResponseFormat.JSON,
            response_schema=self._response_schema,
            temperature=0.0,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "purpose": "route",
                "prompt_version": self._prompt_version,
            },
        )
        result = await self._model_gateway.generate(
            request,
            context.invocation,
            deadline_at=context.invocation.deadline_at,
            parent=context.invocation.trace_context,
            trace_enabled=context.invocation.request.options.trace,
        )
        decision = self._parse_result(result)
        if decision.source is not RouteSource.MODEL:
            raise RoutingError(
                "model route decision must declare model source",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self.router_id,
                    "reason": "invalid_decision_source",
                },
            )
        return self._decision_validator.validate(decision, context)

    def _prompt_payload(self, context: RoutingContext) -> dict[str, object]:
        constraints = context.constraints
        routable_catalog = tuple(
            descriptor
            for descriptor in context.catalog_snapshot
            if descriptor.type in {CapabilityType.AGENT, CapabilityType.TOOL}
        )
        catalog_ids = {descriptor.id for descriptor in routable_catalog}

        allowed_modes = set(_STAGE3B_ROUTABLE_MODES)
        if context.requested_mode is not ExecutionMode.AUTO:
            allowed_modes.intersection_update({context.requested_mode})
        if constraints.allowed_modes is not None:
            allowed_modes.intersection_update(constraints.allowed_modes)

        allowed_capabilities = set(catalog_ids)
        if constraints.allowed_capability_ids is not None:
            allowed_capabilities.intersection_update(constraints.allowed_capability_ids)

        available_planners = set(self._decision_validator.planner_ids)
        if constraints.allowed_planner_ids is not None:
            available_planners.intersection_update(constraints.allowed_planner_ids)

        return {
            "request_summary": context.request_summary.model_dump(mode="json"),
            "requested_mode": context.requested_mode.value,
            "allowed_modes": sorted(mode.value for mode in allowed_modes),
            "capability_catalog": [
                descriptor.model_dump(mode="json") for descriptor in routable_catalog
            ],
            "allowed_capability_ids": sorted(allowed_capabilities),
            "available_planner_ids": sorted(available_planners),
            "route_decision_schema": self._response_schema,
        }

    def _parse_result(self, result: GenerateResult) -> RouteDecision:
        if not isinstance(result, GenerateResult):
            raise RoutingError(
                "model gateway returned an invalid route result",
                code=ErrorCode.ROUTE_MODEL_FAILED,
                details={
                    "router_id": self.router_id,
                    "model": self._route_model_capability_id,
                    "cause_code": "HARNESS.MODEL.INVALID_RESULT",
                },
            )
        if result.status is GenerateStatus.FAILED:
            cause_code = (
                result.error.code if result.error is not None else "HARNESS.MODEL.INVALID_RESULT"
            )
            raise RoutingError(
                "model route generation failed",
                code=ErrorCode.ROUTE_MODEL_FAILED,
                details={
                    "router_id": self.router_id,
                    "model": self._route_model_capability_id,
                    "cause_code": cause_code,
                },
                retryable=(result.error.retryable if result.error is not None else False),
            )

        output = result.output
        if output is None or output.type is not ModelResponseFormat.JSON:
            self._raise_invalid_output("model route output must be a JSON object")
        data = output.data
        if not isinstance(data, Mapping):
            self._raise_invalid_output("model route output must be a JSON object")
        try:
            return RouteDecision.model_validate(dict(data))
        except ValidationError as exc:
            raise RoutingError(
                "model returned an invalid route decision",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self.router_id,
                    "reason": "invalid_model_output",
                    "issue_count": exc.error_count(),
                },
            ) from exc
        except (TypeError, ValueError) as exc:
            raise RoutingError(
                "model returned an invalid route decision",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self.router_id,
                    "reason": "invalid_model_output",
                    "cause_type": type(exc).__name__,
                },
            ) from exc

    def _raise_invalid_output(self, message: str) -> None:
        raise RoutingError(
            message,
            code=ErrorCode.ROUTE_INVALID_DECISION,
            details={
                "router_id": self.router_id,
                "reason": "invalid_model_output",
            },
        )
