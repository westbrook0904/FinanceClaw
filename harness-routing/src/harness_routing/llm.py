"""通过 strict structured generation 只补全未知路由字段的 LLMRouter。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ContractModel,
    ErrorCode,
    ExecutionMode,
    RouteDecision,
    RouteSource,
    RouteType,
    RoutingError,
    StructuredOutputSpec,
    StructuredOutputStrictness,
    UnsupportedStructuredOutputBehavior,
)
from harness_contracts.base import NonEmptyString
from harness_model import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelGateway,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
    StructuredGenerationAdapter,
)
from pydantic import Field, ValidationError, model_validator

from .models import RoutingContext
from .router import Router
from .validation import RouteDecisionValidator

_SYSTEM_PROMPT = """You are a routing completion component inside a controlled Harness.
Return exactly one JSON object accepted by the enforced response schema.
Fill only the unknown fields named in the input. The Harness owns every omitted field.
FAST means direct invocation of one supplied capability; PLAN means a Planner is required later.
Never output source, route_type, requested_mode, planner, provider, plugin, credentials,
or metadata.
Do not call tools, execute capabilities, or invent unavailable targets."""

_ROUTABLE_MODES = frozenset({ExecutionMode.FAST, ExecutionMode.PLAN})
_MODEL_OUTPUT_INVALID_CODES = frozenset(
    {
        ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID.value,
        ErrorCode.MODEL_STRUCTURED_OUTPUT_SCHEMA_INVALID.value,
    }
)


class _RouteIntentDraft(ContractModel):
    """AUTO 仍有歧义时，模型可补全的最小路由意图。"""

    mode: Literal[ExecutionMode.FAST, ExecutionMode.PLAN]
    capability_id: NonEmptyString | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")

    @model_validator(mode="after")
    def validate_shape(self) -> _RouteIntentDraft:
        if self.mode is ExecutionMode.FAST and self.capability_id is None:
            raise ValueError("FAST route intent requires capability_id")
        if self.mode is ExecutionMode.PLAN and self.capability_id is not None:
            raise ValueError("PLAN route intent forbids capability_id")
        return self


class _FastCapabilityDraft(ContractModel):
    """FAST 已由请求或 Policy 固定时，模型只能补全 Capability。"""

    capability_id: NonEmptyString
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")


type _RouteDraft = _RouteIntentDraft | _FastCapabilityDraft
type _RouteDraftType = type[_RouteIntentDraft] | type[_FastCapabilityDraft]


class LLMRouter(Router):
    """通过模型补全静态路由仍未知的字段，并由 Harness 物化最终决策。"""

    def __init__(
        self,
        model_gateway: ModelGateway | StructuredGenerationAdapter,
        *,
        route_model_capability_id: str,
        decision_validator: RouteDecisionValidator,
        router_id: str = "llm-router",
        prompt_version: str = "route-v2",
        max_output_tokens: int | None = None,
    ) -> None:
        if isinstance(model_gateway, StructuredGenerationAdapter):
            generation_adapter = model_gateway
        elif isinstance(model_gateway, ModelGateway):
            generation_adapter = StructuredGenerationAdapter(model_gateway)
        else:
            raise TypeError("model_gateway must be ModelGateway or StructuredGenerationAdapter")
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

        self._generation_adapter = generation_adapter
        self._model_gateway = generation_adapter.gateway
        self._route_model_capability_id = route_model_capability_id
        self._decision_validator = decision_validator
        self._router_id = router_id
        self._prompt_version = prompt_version
        self._max_output_tokens = max_output_tokens
        self._intent_schema = _RouteIntentDraft.model_json_schema()
        self._fast_capability_schema = _FastCapabilityDraft.model_json_schema()

    @property
    def router_id(self) -> str:
        return self._router_id

    @property
    def model_gateway(self) -> ModelGateway:
        return self._model_gateway

    @property
    def generation_adapter(self) -> StructuredGenerationAdapter:
        return self._generation_adapter

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

        allowed_modes = self._allowed_modes(context)
        if not allowed_modes:
            raise RoutingError(
                "routing policy leaves no available execution mode",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={"router_id": self.router_id},
            )

        deterministic = self._materialize_known_decision(context, allowed_modes)
        if deterministic is not None:
            return self._decision_validator.validate(deterministic, context)

        draft_type, schema, schema_name, unknown_fields = self._completion_contract(allowed_modes)
        request = GenerateRequest(
            model=self._route_model_capability_id,
            messages=(
                ModelMessage(role=ModelRole.SYSTEM, content=_SYSTEM_PROMPT),
                ModelMessage(
                    role=ModelRole.USER,
                    content=json.dumps(
                        self._prompt_payload(
                            context,
                            allowed_modes=allowed_modes,
                            unknown_fields=unknown_fields,
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            ),
            response_format=ModelResponseFormat.JSON,
            structured_output=StructuredOutputSpec(
                name=schema_name,
                schema=schema,
                strictness=StructuredOutputStrictness.REQUIRED,
                on_unsupported=UnsupportedStructuredOutputBehavior.FAIL,
            ),
            temperature=0.0,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "purpose": "route",
                "prompt_version": self._prompt_version,
            },
        )
        result = await self._generation_adapter.generate(
            request,
            context.invocation,
            deadline_at=context.invocation.deadline_at,
            parent=context.invocation.trace_context,
            trace_enabled=context.invocation.request.options.trace,
        )
        draft = self._parse_result(result, draft_type)
        decision = self._materialize_model_decision(draft, allowed_modes)
        return self._decision_validator.validate(decision, context)

    def _prompt_payload(
        self,
        context: RoutingContext,
        *,
        allowed_modes: frozenset[ExecutionMode],
        unknown_fields: tuple[str, ...],
    ) -> dict[str, object]:
        routable_catalog = tuple(
            descriptor
            for descriptor in context.catalog_snapshot
            if descriptor.type in {CapabilityType.AGENT, CapabilityType.TOOL}
        )
        catalog_ids = {descriptor.id for descriptor in routable_catalog}
        allowed_capabilities = set(catalog_ids)
        if context.constraints.allowed_capability_ids is not None:
            allowed_capabilities.intersection_update(context.constraints.allowed_capability_ids)

        payload: dict[str, object] = {
            "request_summary": context.request_summary.model_dump(mode="json"),
            "routing_task": (
                "choose_execution_path"
                if ExecutionMode.PLAN in allowed_modes
                else "select_direct_capability"
            ),
            "unknown_fields": unknown_fields,
            "capability_catalog": [
                _safe_descriptor_projection(descriptor)
                for descriptor in routable_catalog
                if descriptor.id in allowed_capabilities
            ],
            "allowed_capability_ids": sorted(allowed_capabilities),
        }
        if len(allowed_modes) > 1:
            payload["allowed_modes"] = sorted(mode.value for mode in allowed_modes)
        return payload

    def _parse_result(
        self,
        result: GenerateResult,
        draft_type: _RouteDraftType,
    ) -> _RouteDraft:
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
            if cause_code in _MODEL_OUTPUT_INVALID_CODES:
                self._raise_invalid_output(
                    "model returned fields outside the route completion schema"
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
            return draft_type.model_validate(dict(data))
        except ValidationError as exc:
            raise RoutingError(
                "model returned an invalid route completion",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self.router_id,
                    "reason": "invalid_model_output",
                    "issue_count": exc.error_count(),
                },
            ) from exc
        except (TypeError, ValueError) as exc:
            raise RoutingError(
                "model returned an invalid route completion",
                code=ErrorCode.ROUTE_INVALID_DECISION,
                details={
                    "router_id": self.router_id,
                    "reason": "invalid_model_output",
                    "cause_type": type(exc).__name__,
                },
            ) from exc

    def _completion_contract(
        self,
        allowed_modes: frozenset[ExecutionMode],
    ) -> tuple[_RouteDraftType, dict[str, object], str, tuple[str, ...]]:
        if allowed_modes == {ExecutionMode.FAST}:
            return (
                _FastCapabilityDraft,
                self._fast_capability_schema,
                "route_capability_v2",
                ("capability_id", "confidence", "reason_code"),
            )
        return (
            _RouteIntentDraft,
            self._intent_schema,
            "route_intent_v2",
            ("mode", "capability_id", "confidence", "reason_code"),
        )

    @staticmethod
    def _allowed_modes(context: RoutingContext) -> frozenset[ExecutionMode]:
        allowed_modes = set(_ROUTABLE_MODES)
        if context.requested_mode is not ExecutionMode.AUTO:
            allowed_modes.intersection_update({context.requested_mode})
        if context.constraints.forced_mode is not None:
            allowed_modes.intersection_update({context.constraints.forced_mode})
        if context.constraints.allowed_modes is not None:
            allowed_modes.intersection_update(context.constraints.allowed_modes)
        return frozenset(allowed_modes)

    @staticmethod
    def _materialize_known_decision(
        context: RoutingContext,
        allowed_modes: frozenset[ExecutionMode],
    ) -> RouteDecision | None:
        target = context.request_summary.target_capability
        if target is not None:
            if ExecutionMode.FAST not in allowed_modes:
                raise RoutingError(
                    "explicit target conflicts with the trusted route scope",
                    code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                    details={"mode": ExecutionMode.FAST.value},
                )
            return RouteDecision(
                mode=ExecutionMode.FAST,
                route_type=RouteType.DIRECT_CAPABILITY,
                source=RouteSource.REQUEST,
                capability_id=target,
                confidence=1.0,
                reason_code="EXPLICIT_TARGET",
            )
        if allowed_modes == {ExecutionMode.PLAN}:
            source = (
                RouteSource.REQUEST
                if context.requested_mode is ExecutionMode.PLAN
                else RouteSource.POLICY
            )
            return RouteDecision(
                mode=ExecutionMode.PLAN,
                route_type=RouteType.GENERATED_PLAN,
                source=source,
                confidence=1.0,
                reason_code=(
                    "REQUEST_MODE_PLAN" if source is RouteSource.REQUEST else "POLICY_SINGLE_MODE"
                ),
            )
        return None

    @staticmethod
    def _materialize_model_decision(
        draft: _RouteDraft,
        allowed_modes: frozenset[ExecutionMode],
    ) -> RouteDecision:
        if isinstance(draft, _FastCapabilityDraft):
            mode = ExecutionMode.FAST
            capability_id = draft.capability_id
        else:
            mode = draft.mode
            capability_id = draft.capability_id
        if mode not in allowed_modes:
            raise RoutingError(
                "model selected a mode outside the trusted route scope",
                code=ErrorCode.ROUTE_MODE_NOT_ALLOWED,
                details={"mode": mode.value},
            )
        return RouteDecision(
            mode=mode,
            route_type=(
                RouteType.DIRECT_CAPABILITY
                if mode is ExecutionMode.FAST
                else RouteType.GENERATED_PLAN
            ),
            source=RouteSource.MODEL,
            capability_id=capability_id,
            confidence=draft.confidence,
            reason_code=draft.reason_code,
        )

    def _raise_invalid_output(self, message: str) -> None:
        raise RoutingError(
            message,
            code=ErrorCode.ROUTE_INVALID_DECISION,
            details={
                "router_id": self.router_id,
                "reason": "invalid_model_output",
            },
        )


def _safe_descriptor_projection(descriptor: CapabilityDescriptor) -> dict[str, object]:
    payload = descriptor.model_dump(mode="json")
    return {
        "id": payload["id"],
        "name": payload["name"],
        "type": payload["type"],
        "version": payload["version"],
        "input_schema": payload["input_schema"],
        "output_schema": payload["output_schema"],
        "execution_profile": payload["execution_profile"],
        "tags": sorted(payload["tags"]),
    }
