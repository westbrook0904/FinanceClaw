"""通过 ModelGateway 生成 PlanDraft、但不获得任何执行权的 LLMPlanner。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionPlan,
    PlanningError,
    PlanNode,
    PlanNodeKind,
    StructuredOutputSpec,
    StructuredOutputStrictness,
    UnsupportedStructuredOutputBehavior,
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
from .identity import PlanIdentityFactory, PlanIdFactory, PlanMaterializer, PlanTemplate
from .models import PlanningAttempt, PlanningAttemptObserver, PlanValidationError
from .planner import Planner, validate_planner_id, validate_planner_output
from .repair import (
    MAX_REPAIR_ERRORS,
    RepairablePlanningFailure,
    RepairFeedback,
    bounded_location_part,
    bounded_repair_value,
    output_hash,
    safe_plan_issue,
)
from .validator import PlanValidator

type Clock = Callable[[], datetime]

_SYSTEM_PROMPT = """You are a planning component inside a controlled Harness.
Return exactly one JSON object matching the supplied PlanDraft schema.
Create a concrete DAG that achieves the supplied goal using only allowed capability IDs.
Never output plan_id, revision, plan metadata, provider IDs, plugin IDs, credentials, or state.
Do not call tools or execute capabilities. Planning is your only responsibility."""

class LLMPlanner(Planner):
    """使用逻辑 Model Capability 自主生成并验证一次 ExecutionPlan。"""

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        planner_model_capability_id: str,
        validator: PlanValidator,
        planner_id: str = "llm-planner",
        prompt_version: str = "planner-v2",
        max_output_tokens: int | None = None,
        plan_id_factory: PlanIdFactory | None = None,
        allowed_capability_ids: Iterable[str] | None = None,
        attempt_observer: PlanningAttemptObserver | None = None,
        clock: Clock | None = None,
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
        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._configured_capability_ids = _validate_capability_scope(allowed_capability_ids)

        self._model_gateway = model_gateway
        self._validator = validator
        self._max_output_tokens = max_output_tokens
        self._compatibility_materializer = PlanMaterializer(
            PlanIdentityFactory(plan_id_factory)
        )
        self._attempt_observer = attempt_observer
        self._clock = clock or (lambda: datetime.now(UTC))
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
        plan = self._compatibility_materializer.materialize(
            template,
            context.invocation,
            planner_id=self.planner_id,
            trusted_metadata={"prompt_version": self._prompt_version},
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

        deadline_at = _effective_deadline(context)
        allowed_capability_ids = self._allowed_capability_ids(context)
        base_payload = self._prompt_payload(
            context,
            allowed_capability_ids,
            effective_deadline_at=deadline_at,
        )
        previous_output: object | None = None
        repair_feedback: RepairFeedback | None = None

        for attempt in range(1, context.constraints.max_plan_attempts + 1):
            self._ensure_deadline(deadline_at, completed_attempts=attempt - 1)
            kind = "initial" if attempt == 1 else "repair"
            request = self._generation_request(
                base_payload,
                attempt=attempt,
                previous_output=previous_output,
                repair_feedback=repair_feedback,
            )
            result = await self._model_gateway.generate(
                request,
                context.invocation,
                deadline_at=deadline_at,
                parent=context.invocation.trace_context,
                trace_enabled=context.invocation.request.options.trace,
            )
            attempt_output_hash = output_hash(result)
            try:
                output, response_format = self._require_generation_output(result)
            except PlanningError:
                await self._observe_attempt(
                    result,
                    attempt=attempt,
                    kind=kind,
                    output_hash=attempt_output_hash,
                    validation_codes=(ErrorCode.PLANNER_MODEL_FAILED.value,),
                    invocation_observer=attempt_observer,
                )
                raise

            try:
                draft = self._parse_draft(output, response_format=response_format)
                self._validate_draft_guards(
                    draft,
                    context,
                    allowed_capability_ids=allowed_capability_ids,
                    effective_deadline_at=deadline_at,
                )
                template = self._to_plan_template(draft)
                template = self._validate_generated_template(template)
            except RepairablePlanningFailure as failure:
                repair_feedback = failure.feedback
                previous_output = bounded_repair_value(output)
                await self._observe_attempt(
                    result,
                    attempt=attempt,
                    kind=kind,
                    output_hash=attempt_output_hash,
                    validation_codes=repair_feedback.validation_codes,
                    repair_scheduled=attempt < context.constraints.max_plan_attempts,
                    invocation_observer=attempt_observer,
                )
                if attempt >= context.constraints.max_plan_attempts:
                    raise PlanningError(
                        "plan repair attempts exhausted",
                        code=ErrorCode.PLANNER_REPAIR_EXHAUSTED,
                        details={
                            "planner_id": self.planner_id,
                            "attempt_count": attempt,
                            "validation_codes": repair_feedback.validation_codes,
                        },
                    ) from failure.error
                continue
            except PlanningError as exc:
                await self._observe_attempt(
                    result,
                    attempt=attempt,
                    kind=kind,
                    output_hash=attempt_output_hash,
                    validation_codes=(str(exc.code),),
                    invocation_observer=attempt_observer,
                )
                raise

            await self._observe_attempt(
                result,
                attempt=attempt,
                kind=kind,
                output_hash=attempt_output_hash,
                validation_codes=(),
                invocation_observer=attempt_observer,
            )
            return template

        raise AssertionError("planning attempt loop must return or raise")

    def _generation_request(
        self,
        base_payload: dict[str, object],
        *,
        attempt: int,
        previous_output: object | None,
        repair_feedback: RepairFeedback | None,
    ) -> GenerateRequest:
        payload = dict(base_payload)
        if repair_feedback is not None:
            payload["repair"] = {
                "attempt": attempt,
                "kind": "repair",
                "previous_plan_draft": previous_output,
                "parse_errors": repair_feedback.parse_errors,
                "plan_validation_issues": repair_feedback.plan_issues,
                "planning_guard_issues": repair_feedback.guard_issues,
                "validation_codes": repair_feedback.validation_codes,
            }
        return GenerateRequest(
            model=self._planner_model_capability_id,
            messages=(
                ModelMessage(role=ModelRole.SYSTEM, content=_SYSTEM_PROMPT),
                ModelMessage(
                    role=ModelRole.USER,
                    content=json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            ),
            response_format=ModelResponseFormat.JSON,
            structured_output=StructuredOutputSpec(
                name="plan_draft_v2",
                schema=self._response_schema,
                strictness=StructuredOutputStrictness.REQUIRED,
                on_unsupported=UnsupportedStructuredOutputBehavior.FAIL,
            ),
            temperature=0.0,
            max_output_tokens=self._max_output_tokens,
            metadata={"purpose": "plan", "prompt_version": self._prompt_version},
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

    def _require_generation_output(
        self,
        result: GenerateResult,
    ) -> tuple[object, ModelResponseFormat]:
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
        if output is None:
            raise PlanningError(
                "model gateway returned a successful result without output",
                code=ErrorCode.PLANNER_MODEL_FAILED,
                details={
                    "planner_id": self.planner_id,
                    "model": self._planner_model_capability_id,
                    "cause_code": "HARNESS.MODEL.INVALID_RESULT",
                },
            )
        return output.data, output.type

    def _parse_draft(
        self,
        output: object,
        *,
        response_format: ModelResponseFormat,
    ) -> PlanDraft:
        if response_format is not ModelResponseFormat.JSON or not isinstance(output, Mapping):
            error = PlanningError(
                "model plan output must be a JSON object",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "invalid_model_output",
                },
            )
            raise RepairablePlanningFailure(
                error,
                RepairFeedback(
                    validation_codes=("DRAFT.INVALID_JSON_OBJECT",),
                    parse_errors=({"type": "json_object_required", "location": ()},),
                ),
            ) from error
        try:
            return PlanDraft.model_validate(dict(output))
        except ValidationError as exc:
            parse_errors = tuple(
                {
                    "type": str(issue["type"]),
                    "location": tuple(bounded_location_part(item) for item in issue["loc"]),
                }
                for issue in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )[:MAX_REPAIR_ERRORS]
            )
            validation_codes = tuple(
                sorted({f"DRAFT.PARSE.{issue['type']}" for issue in parse_errors})
            )
            error = PlanningError(
                "model returned an invalid plan draft",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "invalid_model_output",
                    "issue_count": exc.error_count(),
                },
            )
            raise RepairablePlanningFailure(
                error,
                RepairFeedback(
                    validation_codes=validation_codes,
                    parse_errors=parse_errors,
                ),
            ) from exc
        except (TypeError, ValueError) as exc:
            error = PlanningError(
                "model returned an invalid plan draft",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "invalid_model_output",
                    "cause_type": type(exc).__name__,
                },
            )
            raise RepairablePlanningFailure(
                error,
                RepairFeedback(
                    validation_codes=(f"DRAFT.PARSE.{type(exc).__name__}",),
                    parse_errors=({"type": type(exc).__name__, "location": ()},),
                ),
            ) from exc

    def _validate_generated_template(self, template: PlanTemplate) -> PlanTemplate:
        try:
            return self._validator.validate_template(template)
        except PlanValidationError as exc:
            validation_codes = tuple(sorted({issue.code.value for issue in exc.issues}))
            error = PlanningError(
                "planner returned an invalid execution plan",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "issue_count": len(exc.issues),
                    "validation_codes": validation_codes,
                },
            )
            raise RepairablePlanningFailure(
                error,
                RepairFeedback(
                    validation_codes=validation_codes,
                    plan_issues=tuple(
                        safe_plan_issue(issue) for issue in exc.issues[:MAX_REPAIR_ERRORS]
                    ),
                ),
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

    async def _observe_attempt(
        self,
        result: object,
        *,
        attempt: int,
        kind: str,
        output_hash: str | None,
        validation_codes: tuple[str, ...],
        repair_scheduled: bool = False,
        invocation_observer: PlanningAttemptObserver | None = None,
    ) -> None:
        observers: list[PlanningAttemptObserver] = []
        for observer in (self._attempt_observer, invocation_observer):
            if observer is not None and all(observer is not item for item in observers):
                observers.append(observer)
        if not observers:
            return
        usage = result.usage if isinstance(result, GenerateResult) else None
        summary = PlanningAttempt(
            attempt=attempt,
            kind=kind,
            provider_id=(result.provider_id if isinstance(result, GenerateResult) else None),
            prompt_version=self._prompt_version,
            output_hash=output_hash,
            validation_codes=validation_codes,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            repair_scheduled=repair_scheduled,
        )
        for observer in observers:
            observation = observer(summary)
            if inspect.isawaitable(observation):
                await observation

    def _ensure_deadline(
        self,
        deadline_at: datetime | None,
        *,
        completed_attempts: int,
    ) -> None:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise TypeError("clock must return a timezone-aware datetime")
        if deadline_at is not None and now >= deadline_at:
            raise PlanningError(
                "planning deadline has expired",
                code=ErrorCode.PLANNER_DEADLINE_EXCEEDED,
                details={
                    "planner_id": self.planner_id,
                    "attempt_count": completed_attempts,
                },
            )

    def _validate_draft_guards(
        self,
        draft: PlanDraft,
        context: PlanningContext,
        *,
        allowed_capability_ids: frozenset[str],
        effective_deadline_at: datetime | None,
    ) -> None:
        if len(draft.nodes) > context.constraints.max_plan_nodes:
            error = PlanningError(
                "generated plan exceeds the node limit",
                code=ErrorCode.PLANNER_PLAN_TOO_LARGE,
                details={
                    "planner_id": self.planner_id,
                    "node_count": len(draft.nodes),
                    "max_plan_nodes": context.constraints.max_plan_nodes,
                },
            )
            self._raise_guard_failure(
                error,
                code=ErrorCode.PLANNER_PLAN_TOO_LARGE.value,
                field="nodes",
            )

        disallowed = sorted(
            {
                node.capability_id
                for node in draft.nodes
                if node.kind is PlanNodeKind.CAPABILITY
                and node.capability_id not in allowed_capability_ids
            }
        )
        if disallowed:
            error = PlanningError(
                "generated plan references capabilities outside the allowed scope",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "capability_not_allowed",
                    "capability_ids": disallowed,
                },
            )
            self._raise_guard_failure(
                error,
                code="PLANNING.CAPABILITY_NOT_ALLOWED",
                field="nodes.capability_id",
                reference=",".join(disallowed),
            )

        draft_deadline = draft.budget.deadline_at
        if (
            draft_deadline is not None
            and effective_deadline_at is not None
            and draft_deadline > effective_deadline_at
        ):
            error = PlanningError(
                "generated plan deadline exceeds the request deadline",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": self.planner_id,
                    "reason": "deadline_exceeds_request",
                },
            )
            self._raise_guard_failure(
                error,
                code="PLANNING.DEADLINE_EXCEEDS_REQUEST",
                field="budget.deadline_at",
            )

    @staticmethod
    def _raise_guard_failure(
        error: PlanningError,
        *,
        code: str,
        field: str,
        reference: str | None = None,
    ) -> None:
        issue: dict[str, object] = {"code": code, "field": field}
        if reference is not None:
            issue["reference"] = reference
        raise RepairablePlanningFailure(
            error,
            RepairFeedback(
                validation_codes=(code,),
                guard_issues=(issue,),
            ),
        ) from error

    def _to_plan_template(
        self,
        draft: PlanDraft,
    ) -> PlanTemplate:
        return PlanTemplate(
            budget=draft.budget,
            failure_policy=draft.failure_policy,
            nodes=tuple(
                PlanNode(
                    node_id=node.node_id,
                    kind=node.kind,
                    capability=node.capability_id,
                    input_mapping=node.input_mapping,
                    failure_policy=node.failure_intent,
                )
                for node in draft.nodes
            ),
            edges=draft.edges,
            outputs=draft.outputs,
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
