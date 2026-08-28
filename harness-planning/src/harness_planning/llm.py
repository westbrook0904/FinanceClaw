"""通过 ModelGateway 生成 PlanDraft、但不获得任何执行权的 LLMPlanner。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionPlan,
    PlanningError,
    PlanNodeKind,
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

from .context import PlanningContext
from .draft import PlanDraft
from .planner import Planner, validate_planner_id, validate_planner_output
from .validator import PlanValidator

type PlanIdFactory = Callable[[], str]

_SYSTEM_PROMPT = """You are a planning component inside a controlled Harness.
Return exactly one JSON object matching the supplied PlanDraft schema.
Create a concrete DAG that achieves the supplied goal using only allowed capability IDs.
Never output plan_id, revision, plan metadata, provider IDs, plugin IDs, credentials, or state.
Do not call tools or execute capabilities. Planning is your only responsibility."""

_RESERVED_NODE_METADATA_KEYS = frozenset(
    {
        "plan_id",
        "revision",
        "planner_id",
        "prompt_version",
        "request_id",
        "provider_id",
        "plugin_id",
        "execution_state",
        "checkpoint_state",
        "approval_grant",
    }
)


class LLMPlanner(Planner):
    """使用逻辑 Model Capability 自主生成并验证一次 ExecutionPlan。"""

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        planner_model_capability_id: str,
        validator: PlanValidator,
        planner_id: str = "llm-planner",
        prompt_version: str = "planner-v1",
        max_output_tokens: int | None = None,
        plan_id_factory: PlanIdFactory | None = None,
        allowed_capability_ids: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(model_gateway, ModelGateway):
            raise TypeError("model_gateway must be ModelGateway")
        self._planner_id = validate_planner_id(planner_id)
        self._planner_model_capability_id = _validate_non_empty_string(
            "planner_model_capability_id",
            planner_model_capability_id,
        )
        self._prompt_version = _validate_non_empty_string(
            "prompt_version",
            prompt_version,
        )
        if not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise TypeError("max_output_tokens must be a positive integer when provided")
        if plan_id_factory is not None and not callable(plan_id_factory):
            raise TypeError("plan_id_factory must be callable")
        self._configured_capability_ids = _validate_capability_scope(allowed_capability_ids)

        self._model_gateway = model_gateway
        self._validator = validator
        self._max_output_tokens = max_output_tokens
        self._plan_id_factory = plan_id_factory or (lambda: f"plan-{uuid4().hex}")
        self._response_schema = PlanDraft.model_json_schema()

    @property
    def planner_id(self) -> str:
        return self._planner_id

    @property
    def model_gateway(self) -> ModelGateway:
        return self._model_gateway

    @property
    def planner_model_capability_id(self) -> str:
        return self._planner_model_capability_id

    @property
    def validator(self) -> PlanValidator:
        return self._validator

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def configured_capability_ids(self) -> frozenset[str] | None:
        return self._configured_capability_ids

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        if not isinstance(context, PlanningContext):
            raise TypeError("context must be PlanningContext")

        deadline_at = _effective_deadline(context)
        if deadline_at is not None and datetime.now(UTC) >= deadline_at:
            raise PlanningError(
                "planning deadline has already expired",
                code=ErrorCode.PLANNER_DEADLINE_EXCEEDED,
                details={"planner_id": self.planner_id},
            )

        allowed_capability_ids = self._allowed_capability_ids(context)
        request = GenerateRequest(
            model=self._planner_model_capability_id,
            messages=(
                ModelMessage(role=ModelRole.SYSTEM, content=_SYSTEM_PROMPT),
                ModelMessage(
                    role=ModelRole.USER,
                    content=json.dumps(
                        self._prompt_payload(
                            context,
                            allowed_capability_ids,
                            effective_deadline_at=deadline_at,
                        ),
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
            metadata={"purpose": "plan", "prompt_version": self._prompt_version},
        )
        result = await self._model_gateway.generate(
            request,
            context.invocation,
            deadline_at=deadline_at,
            parent=context.invocation.trace_context,
            trace_enabled=context.invocation.request.options.trace,
        )
        draft = self._parse_result(result)
        self._validate_draft_guards(
            draft,
            context,
            allowed_capability_ids=allowed_capability_ids,
            effective_deadline_at=deadline_at,
        )
        plan = self._to_execution_plan(draft, context)
        return validate_planner_output(
            plan,
            self._validator,
            planner_id=self.planner_id,
        )

    def _prompt_payload(
        self,
        context: PlanningContext,
        allowed_capability_ids: frozenset[str],
        *,
        effective_deadline_at: datetime | None,
    ) -> dict[str, object]:
        catalog = tuple(
            _safe_descriptor_projection(descriptor)
            for descriptor in context.catalog_snapshot
            if descriptor.id in allowed_capability_ids
        )
        return {
            "goal": context.goal.model_dump(mode="json"),
            "capability_catalog": catalog,
            "allowed_capability_ids": sorted(allowed_capability_ids),
            "planning_constraints": {
                "max_plan_nodes": context.constraints.max_plan_nodes,
                "deadline_at": (
                    effective_deadline_at.isoformat() if effective_deadline_at is not None else None
                ),
            },
            "plan_draft_schema": self._response_schema,
        }

    def _allowed_capability_ids(self, context: PlanningContext) -> frozenset[str]:
        allowed = {
            descriptor.id
            for descriptor in context.catalog_snapshot
            if descriptor.type in {CapabilityType.AGENT, CapabilityType.TOOL}
        }
        if context.constraints.allowed_capability_ids is not None:
            allowed.intersection_update(context.constraints.allowed_capability_ids)
        if self._configured_capability_ids is not None:
            allowed.intersection_update(self._configured_capability_ids)
        return frozenset(allowed)

    def _parse_result(self, result: GenerateResult) -> PlanDraft:
        if not isinstance(result, GenerateResult):
            raise PlanningError(
                "model gateway returned an invalid planning result",
                code=ErrorCode.PLANNER_MODEL_FAILED,
                details={
                    "planner_id": self.planner_id,
                    "model": self._planner_model_capability_id,
                    "cause_code": "HARNESS.MODEL.INVALID_RESULT",
                },
            )
        if result.status is GenerateStatus.FAILED:
            cause_code = (
                result.error.code if result.error is not None else "HARNESS.MODEL.INVALID_RESULT"
            )
            raise PlanningError(
                "model plan generation failed",
                code=ErrorCode.PLANNER_MODEL_FAILED,
                details={
                    "planner_id": self.planner_id,
                    "model": self._planner_model_capability_id,
                    "cause_code": cause_code,
                },
                retryable=result.error.retryable if result.error is not None else False,
            )

        output = result.output
        if output is None or output.type is not ModelResponseFormat.JSON:
            self._raise_invalid_output("model plan output must be a JSON object")
        data = output.data
        if not isinstance(data, Mapping):
            self._raise_invalid_output("model plan output must be a JSON object")
        try:
            return PlanDraft.model_validate(dict(data))
        except ValidationError as exc:
            raise PlanningError(
                "model returned an invalid plan draft",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "invalid_model_output",
                    "issue_count": exc.error_count(),
                },
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PlanningError(
                "model returned an invalid plan draft",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "invalid_model_output",
                    "cause_type": type(exc).__name__,
                },
            ) from exc

    def _validate_draft_guards(
        self,
        draft: PlanDraft,
        context: PlanningContext,
        *,
        allowed_capability_ids: frozenset[str],
        effective_deadline_at: datetime | None,
    ) -> None:
        if len(draft.nodes) > context.constraints.max_plan_nodes:
            raise PlanningError(
                "generated plan exceeds the node limit",
                code=ErrorCode.PLANNER_PLAN_TOO_LARGE,
                details={
                    "planner_id": self.planner_id,
                    "node_count": len(draft.nodes),
                    "max_plan_nodes": context.constraints.max_plan_nodes,
                },
            )

        disallowed = sorted(
            {
                node.capability
                for node in draft.nodes
                if node.kind is PlanNodeKind.CAPABILITY
                and node.capability not in allowed_capability_ids
            }
        )
        if disallowed:
            raise PlanningError(
                "generated plan references capabilities outside the allowed scope",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "capability_not_allowed",
                    "capability_ids": disallowed,
                },
            )

        draft_deadline = draft.budget.deadline_at
        if (
            draft_deadline is not None
            and effective_deadline_at is not None
            and draft_deadline > effective_deadline_at
        ):
            raise PlanningError(
                "generated plan deadline exceeds the request deadline",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "deadline_exceeds_request",
                },
            )

        injected_keys = sorted(
            {
                key
                for node in draft.nodes
                for key in node.metadata
                if key in _RESERVED_NODE_METADATA_KEYS
            }
        )
        if injected_keys:
            raise PlanningError(
                "generated plan contains reserved node metadata",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "reserved_metadata",
                    "metadata_keys": injected_keys,
                },
            )

    def _to_execution_plan(
        self,
        draft: PlanDraft,
        context: PlanningContext,
    ) -> ExecutionPlan:
        try:
            plan_id = self._plan_id_factory()
        except Exception as exc:
            raise PlanningError(
                "plan identity generation failed",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PlanningError(
                "plan identity factory returned an invalid plan_id",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={"planner_id": self.planner_id},
            )
        return ExecutionPlan(
            plan_id=plan_id.strip(),
            revision=1,
            budget=draft.budget,
            failure_policy=draft.failure_policy,
            nodes=draft.nodes,
            edges=draft.edges,
            outputs=draft.outputs,
            metadata={
                "planner_id": self.planner_id,
                "prompt_version": self._prompt_version,
                "request_id": context.invocation.request.request_id,
            },
        )

    def _raise_invalid_output(self, message: str) -> None:
        raise PlanningError(
            message,
            code=ErrorCode.PLANNER_INVALID_OUTPUT,
            details={
                "planner_id": self.planner_id,
                "reason": "invalid_model_output",
            },
        )


def _effective_deadline(context: PlanningContext) -> datetime | None:
    values = tuple(
        value
        for value in (context.invocation.deadline_at, context.constraints.deadline_at)
        if value is not None
    )
    return min(values) if values else None


def _safe_descriptor_projection(descriptor: CapabilityDescriptor) -> dict[str, object]:
    return {
        "id": descriptor.id,
        "name": descriptor.name,
        "type": descriptor.type.value,
        "version": descriptor.version,
        "input_schema": descriptor.model_dump(mode="json")["input_schema"],
        "output_schema": descriptor.model_dump(mode="json")["output_schema"],
        "execution_profile": descriptor.execution_profile.model_dump(mode="json"),
        "tags": sorted(descriptor.tags),
    }


def _validate_non_empty_string(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not include surrounding whitespace")
    return value


def _validate_capability_scope(
    values: Iterable[str] | None,
) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise TypeError("allowed_capability_ids must be an iterable of strings")
    scope = frozenset(values)
    if any(not isinstance(value, str) or not value.strip() for value in scope):
        raise TypeError("allowed_capability_ids must contain non-empty strings")
    if any(value != value.strip() for value in scope):
        raise ValueError("allowed_capability_ids must not contain surrounding whitespace")
    return scope
