"""F4b standalone Minimal Explore Loop。"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from harness_context import ContextPipeline, PromptBuilder
from harness_contracts import (
    ActionExecutionState,
    ActionProposal,
    CallCapabilityDraft,
    ContextConsumer,
    ErrorCode,
    ExecutionPlan,
    ExplorationError,
    ExplorationState,
    ExplorationStatus,
    ExplorationTurnDraft,
    FinishDraft,
    HarnessError,
    InvocationContext,
    LiteralBinding,
    NodeExecutionState,
    NodeExecutionStatus,
    Observation,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    ResultEnvelope,
    ResultIssue,
    ResultOutput,
    ResultStatus,
    StructuredOutputSpec,
    StructuredOutputStrictness,
    UnsupportedStructuredOutputBehavior,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
    StructuredGenerationAdapter,
)
from harness_registry import CapabilityCatalog
from harness_runtime import InvocationLifecycle
from harness_trace import Span, SpanType, Tracer
from pydantic import TypeAdapter, ValidationError

from .action import ScopedActionExecutor
from .canonical import (
    action_fingerprint,
    action_proposal_hash,
    canonical_hash,
    exploration_scope_hash,
    result_envelope_hash,
)
from .checkpoint import ExplorationCheckpointValidator

type CheckpointCallback = Callable[[PlanExecutionState], Awaitable[None]]
type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]

_SYSTEM_PROMPT = """You are a bounded exploration component inside a controlled Harness.
Return exactly one JSON object matching the supplied exploration_turn_v1 schema.
Each turn must either call one allowed capability or finish from explicit evidence.
Use only capability IDs and schemas present in context. Never invent evidence references.
Never output plan IDs, provider IDs, plugin IDs, credentials, policy decisions, or state.
Do not claim that an action ran; the Harness alone executes accepted proposals."""


class CancellationView(Protocol):
    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    async def wait(self) -> None: ...


class _SignalCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExplorationOutcome:
    result: ResultEnvelope
    state: PlanExecutionState


class ExplorationEngine:
    """在一个 Harness-owned Exploration 节点内逐轮生成一个受限决策。"""

    def __init__(
        self,
        generation_adapter: StructuredGenerationAdapter,
        context_pipeline: ContextPipeline,
        catalog: CapabilityCatalog,
        action_executor: ScopedActionExecutor,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
        *,
        memory_available: bool,
        checkpoint_validator: ExplorationCheckpointValidator | None = None,
        prompt_builder: PromptBuilder | None = None,
        max_turn_repairs: int = 1,
        max_output_tokens: int | None = None,
        clock: Clock | None = None,
        action_id_factory: IdFactory | None = None,
        observation_id_factory: IdFactory | None = None,
    ) -> None:
        if not isinstance(generation_adapter, StructuredGenerationAdapter):
            raise TypeError("generation_adapter must be StructuredGenerationAdapter")
        if not isinstance(context_pipeline, ContextPipeline):
            raise TypeError("context_pipeline must be ContextPipeline")
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must implement CapabilityCatalog")
        if not isinstance(action_executor, ScopedActionExecutor):
            raise TypeError("action_executor must be ScopedActionExecutor")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if not isinstance(memory_available, bool):
            raise TypeError("memory_available must be bool")
        if not isinstance(max_turn_repairs, int) or isinstance(max_turn_repairs, bool):
            raise TypeError("max_turn_repairs must be an integer")
        if max_turn_repairs < 0 or max_turn_repairs > 3:
            raise ValueError("max_turn_repairs must be between 0 and 3")
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise TypeError("max_output_tokens must be a positive integer when provided")
        if lifecycle.tracer is not tracer:
            raise ValueError("exploration engine components must share one tracer")
        if action_executor.catalog is not catalog:
            raise ValueError("exploration engine and action executor must share one catalog")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if action_id_factory is not None and not callable(action_id_factory):
            raise TypeError("action_id_factory must be callable")
        if observation_id_factory is not None and not callable(observation_id_factory):
            raise TypeError("observation_id_factory must be callable")

        self._generation = generation_adapter
        self._context_pipeline = context_pipeline
        self._catalog = catalog
        self._action_executor = action_executor
        self._tracer = tracer
        self._lifecycle = lifecycle
        self._memory_available = memory_available
        self._checkpoint_validator = checkpoint_validator or ExplorationCheckpointValidator()
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._max_turn_repairs = max_turn_repairs
        self._max_output_tokens = max_output_tokens
        self._clock = clock or (lambda: datetime.now(UTC))
        self._action_id_factory = action_id_factory or (lambda: f"action-{uuid4().hex}")
        self._observation_id_factory = observation_id_factory or (
            lambda: f"observation-{uuid4().hex}"
        )
        self._turn_adapter = TypeAdapter(ExplorationTurnDraft)
        self._turn_schema = self._turn_adapter.json_schema()

    @property
    def context_pipeline(self) -> ContextPipeline:
        return self._context_pipeline

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    @property
    def memory_available(self) -> bool:
        return self._memory_available

    def validate_checkpoint(
        self,
        plan: ExecutionPlan,
        context: InvocationContext,
        state: PlanExecutionState,
    ) -> None:
        """校验 outer/inner checkpoint；terminal resume 也必须经过此边界。"""

        self._checkpoint_validator.validate(self._record_for_validation(plan, context, state))

    async def run(
        self,
        request: Request,
        plan: ExecutionPlan,
        context: InvocationContext,
        *,
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationView,
        checkpoint: CheckpointCallback,
    ) -> ExplorationOutcome:
        node = self._require_plan(plan)
        now = self._now()
        assert node.exploration is not None
        child = ExplorationState(
            exploration_id=node.exploration.exploration_id,
            plan_id=plan.plan_id,
            node_id=node.node_id,
            profile=node.exploration.profile,
            status=ExplorationStatus.RUNNING,
            scope_hash=exploration_scope_hash(node.exploration.profile),
            started_at=now,
            updated_at=now,
        )
        state = PlanExecutionState(
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            status=PlanExecutionStatus.RUNNING,
            nodes={
                node.node_id: NodeExecutionState(
                    node_id=node.node_id,
                    status=NodeExecutionStatus.RUNNING,
                    attempt=1,
                    started_at=now,
                )
            },
            explorations={node.node_id: child},
            started_at=now,
            updated_at=now,
            metadata={"execution_kind": "standalone_exploration"},
        )
        await checkpoint(state)
        return await self._drive(
            request,
            plan,
            node,
            context,
            state,
            resumed=False,
            parent=parent,
            trace_enabled=trace_enabled,
            cancellation=cancellation,
            checkpoint=checkpoint,
        )

    async def resume(
        self,
        request: Request,
        plan: ExecutionPlan,
        context: InvocationContext,
        state: PlanExecutionState,
        *,
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationView,
        checkpoint: CheckpointCallback,
    ) -> ExplorationOutcome:
        node = self._require_plan(plan)
        self.validate_checkpoint(plan, context, state)
        child = state.explorations.get(node.node_id)
        if child is None:
            raise ExplorationError(
                "exploration checkpoint has no started child state",
                code=ErrorCode.EXPLORATION_RESUME_UNSAFE,
                details={"plan_id": plan.plan_id, "node_id": node.node_id},
            )
        if child.status not in {ExplorationStatus.CREATED, ExplorationStatus.RUNNING}:
            if child.final_result is None:
                raise ExplorationError(
                    "terminal exploration checkpoint has no result",
                    code=ErrorCode.EXPLORATION_CHECKPOINT_CORRUPT,
                )
            return ExplorationOutcome(child.final_result, state)
        if child.pending_action_id is not None or any(
            action.status in {"proposed", "running"} for action in child.actions
        ):
            raise ExplorationError(
                "exploration cannot resume across an uncertain action boundary",
                code=ErrorCode.EXPLORATION_RESUME_UNSAFE,
                details={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "pending_action_id": child.pending_action_id,
                },
            )
        return await self._drive(
            request,
            plan,
            node,
            context,
            state,
            resumed=True,
            parent=parent,
            trace_enabled=trace_enabled,
            cancellation=cancellation,
            checkpoint=checkpoint,
        )

    async def _drive(
        self,
        request: Request,
        plan: ExecutionPlan,
        node: PlanNode,
        context: InvocationContext,
        state: PlanExecutionState,
        *,
        resumed: bool,
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationView,
        checkpoint: CheckpointCallback,
    ) -> ExplorationOutcome:
        child = state.explorations[node.node_id]
        span = (
            self._tracer.start_span(
                "exploration.resume" if resumed else "exploration.execute",
                SpanType.EXPLORATION,
                parent=parent,
                attributes={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "exploration_id": child.exploration_id,
                    "profile_id": child.profile.profile_id,
                    "profile_hash": child.profile.profile_hash,
                    "scope_hash": child.scope_hash,
                    "resumed": resumed,
                },
            )
            if trace_enabled
            else None
        )
        invocation = (
            self._lifecycle.with_trace_context(context, span) if span is not None else context
        )
        try:
            if child.profile.memory_required and (
                not self._memory_available
                or invocation.tenant is None
                or invocation.identity is None
            ):
                error = ExplorationError(
                    "exploration profile requires a configured Memory source",
                    code=ErrorCode.EXPLORATION_MEMORY_REQUIRED,
                    details={"profile_id": child.profile.profile_id},
                )
                result = await self._terminal_failure(state, child, error, checkpoint)
                self._lifecycle.finish_from_result(span, result)
                return ExplorationOutcome(result, state)

            repair_codes: tuple[str, ...] = ()
            repairs = 0
            while True:
                if cancellation.cancelled:
                    result = await self._terminal_cancelled(
                        state, child, cancellation.reason, checkpoint
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                if self._deadline_expired(plan, invocation):
                    error = ExplorationError(
                        "exploration deadline exceeded",
                        code=ErrorCode.TIMEOUT,
                        details={"plan_id": plan.plan_id},
                    )
                    result = await self._terminal_failure(state, child, error, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                budget_result = await self._enforce_turn_budget(state, child, checkpoint)
                if budget_result is not None:
                    self._lifecycle.finish_from_result(span, budget_result)
                    return ExplorationOutcome(budget_result, state)

                try:
                    turn, projection_hash, catalog_hash = await self._generate_turn(
                        request,
                        plan,
                        node,
                        invocation,
                        state,
                        child,
                        repair_codes=repair_codes,
                        parent=span,
                        trace_enabled=trace_enabled,
                        cancellation=cancellation,
                        checkpoint=checkpoint,
                    )
                    proposal = None
                    if isinstance(turn, CallCapabilityDraft):
                        proposal = self._validated_proposal(
                            turn,
                            child,
                            projection_hash=projection_hash,
                            catalog_hash=catalog_hash,
                        )
                    else:
                        self._validate_finish(turn, child)
                except _SignalCancelled:
                    result = await self._terminal_cancelled(
                        state, child, cancellation.reason, checkpoint
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                except ExplorationError as exc:
                    if exc.code == str(ErrorCode.EXPLORATION_MODEL_FAILED):
                        result = await self._terminal_failure(state, child, exc, checkpoint)
                        self._lifecycle.finish_from_result(span, result)
                        return ExplorationOutcome(result, state)
                    repairs += 1
                    repair_codes = (str(exc.code),)
                    self._trace_event(
                        span,
                        "exploration.turn.repairing",
                        {"repair": repairs, "validation_codes": list(repair_codes)},
                    )
                    if repairs <= self._max_turn_repairs:
                        continue
                    error = ExplorationError(
                        "exploration turn repair attempts exhausted",
                        code=ErrorCode.EXPLORATION_INVALID_TURN,
                        details={
                            "profile_id": child.profile.profile_id,
                            "validation_codes": list(repair_codes),
                        },
                    )
                    result = await self._terminal_failure(state, child, error, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                except HarnessError as exc:
                    result = await self._terminal_failure(state, child, exc, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                repairs = 0
                repair_codes = ()
                child.usage.steps += 1
                if isinstance(turn, FinishDraft):
                    result = (
                        ResultEnvelope.partial(turn.output, tuple(state.issues))
                        if state.issues
                        else ResultEnvelope.success(turn.output)
                    )
                    result = self._with_metadata(result, plan, child)
                    await self._terminalize(state, child, result, checkpoint)
                    self._trace_event(
                        span,
                        "exploration.turn.completed",
                        {"step": child.usage.steps, "decision": "finish"},
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                assert proposal is not None
                repeated = Counter(
                    action_fingerprint(action.proposal.capability_id, action.proposal.input)
                    for action in child.actions
                )[action_fingerprint(proposal.capability_id, proposal.input)]
                if repeated > child.profile.budget.max_repeated_actions:
                    result = await self._budget_terminal(
                        state,
                        child,
                        ErrorCode.EXPLORATION_REPEATED_ACTION,
                        "exploration repeated action budget exhausted",
                        checkpoint,
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                if (
                    child.usage.action_calls >= child.profile.budget.max_action_calls
                    or len(child.observations) >= child.profile.budget.max_observations
                ):
                    result = await self._budget_terminal(
                        state,
                        child,
                        ErrorCode.EXPLORATION_BUDGET_EXHAUSTED,
                        "exploration action or observation budget exhausted",
                        checkpoint,
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                action = ActionExecutionState(
                    action_id=proposal.action_id,
                    proposal=proposal,
                )
                child.actions.append(action)
                child.usage.action_calls += 1
                child.pending_action_id = action.action_id
                self._touch(state, child)
                await checkpoint(state)
                self._trace_event(
                    span,
                    "action.proposed",
                    {
                        "action_id": action.action_id,
                        "step": proposal.step,
                        "capability_id": proposal.capability_id,
                        "proposal_hash": proposal.proposal_hash,
                    },
                )
                action.status = "running"
                action.started_at = self._now()
                self._touch(state, child)
                await checkpoint(state)
                try:
                    action_result = await self._await_with_cancellation(
                        self._action_executor.execute(
                            proposal,
                            child.profile,
                            invocation,
                            deadline_at=self._effective_deadline(plan, invocation),
                            parent=span,
                            trace_enabled=trace_enabled,
                        ),
                        cancellation,
                    )
                except _SignalCancelled:
                    cancelled = ResultEnvelope.cancelled(metadata={"action_id": action.action_id})
                    self._complete_governed_action(action, cancelled, "cancelled")
                    child.pending_action_id = None
                    result = await self._terminal_cancelled(
                        state, child, cancellation.reason, checkpoint
                    )
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                if action_result.status is ResultStatus.ACCEPTED:
                    if (
                        action_result.continuation is not None
                        and action_result.continuation.approval_id is not None
                    ):
                        error = ExplorationError(
                            "approval waiting is not supported inside minimal exploration",
                            code=ErrorCode.EXPLORATION_APPROVAL_UNSUPPORTED,
                            details={"capability_id": proposal.capability_id},
                        )
                        denied = ResultEnvelope.denied(error.to_detail())
                        self._complete_governed_action(action, denied, "denied")
                        child.pending_action_id = None
                        result = self._with_metadata(denied, plan, child)
                        await self._terminalize(state, child, result, checkpoint)
                        self._lifecycle.finish_from_result(span, result)
                        return ExplorationOutcome(result, state)
                    error = ExplorationError(
                        "SYNC exploration action returned ACCEPTED",
                        code=ErrorCode.EXPLORATION_ASYNC_CONTRACT_VIOLATION,
                        details={
                            "capability_id": proposal.capability_id,
                            "action_id": action.action_id,
                        },
                    )
                    self._complete_governed_action(
                        action,
                        action_result,
                        "orphaned",
                        error_code=str(error.code),
                    )
                    child.pending_action_id = None
                    result = await self._terminal_failure(state, child, error, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                if action_result.status is ResultStatus.DENIED:
                    self._complete_governed_action(action, action_result, "denied")
                    child.pending_action_id = None
                    result = self._with_metadata(action_result, plan, child)
                    await self._terminalize(state, child, result, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)
                if action_result.status is ResultStatus.CANCELLED:
                    self._complete_governed_action(action, action_result, "cancelled")
                    child.pending_action_id = None
                    result = self._with_metadata(action_result, plan, child)
                    await self._terminalize(state, child, result, checkpoint)
                    self._lifecycle.finish_from_result(span, result)
                    return ExplorationOutcome(result, state)

                observation = self._observation(action, action_result)
                action.status = (
                    "succeeded"
                    if action_result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}
                    else "failed"
                )
                action.result = action_result
                action.error_code = (
                    action_result.error.code if action_result.error is not None else None
                )
                action.observation_id = observation.observation_id
                action.completed_at = self._now()
                child.observations.append(observation)
                child.pending_action_id = None
                self._touch(state, child)
                await checkpoint(state)
                self._trace_event(
                    span,
                    "action.completed",
                    {
                        "action_id": action.action_id,
                        "status": action.status,
                        "observation_id": observation.observation_id,
                        "result_hash": observation.result_hash,
                    },
                )
        except asyncio.CancelledError:
            try:
                if child.pending_action_id is not None:
                    pending = next(
                        (
                            action
                            for action in child.actions
                            if action.action_id == child.pending_action_id
                        ),
                        None,
                    )
                    if pending is not None and pending.status in {"proposed", "running"}:
                        self._complete_governed_action(
                            pending,
                            ResultEnvelope.cancelled(metadata={"action_id": pending.action_id}),
                            "cancelled",
                        )
                    child.pending_action_id = None
                await asyncio.shield(
                    self._terminal_cancelled(state, child, "task_cancelled", checkpoint)
                )
            finally:
                self._lifecycle.finish_cancelled(span)
            raise

    async def _generate_turn(
        self,
        request: Request,
        plan: ExecutionPlan,
        node: PlanNode,
        context: InvocationContext,
        state: PlanExecutionState,
        child: ExplorationState,
        *,
        repair_codes: tuple[str, ...],
        parent: Span | None,
        trace_enabled: bool,
        cancellation: CancellationView,
        checkpoint: CheckpointCallback,
    ) -> tuple[CallCapabilityDraft | FinishDraft, str, str]:
        catalog = self._action_catalog(child)
        request_projection = {
            "input_type": request.input.type,
            "goal": self._resolve_goal(request, node),
            **({"repair": {"validation_codes": list(repair_codes)}} if repair_codes else {}),
        }
        bundle = await self._context_pipeline.build(
            context,
            ContextConsumer.EXPLORE,
            request_projection=request_projection,
            capability_catalog=catalog,
            observations=tuple(child.observations),
            suppress_memory_errors=not child.profile.memory_required,
        )
        for error in bundle.issues:
            if not any(
                issue.source == "context.memory" and issue.error == error for issue in state.issues
            ):
                state.issues.append(ResultIssue(source="context.memory", error=error))
        child.context_uses.append(bundle.use_record)
        child.usage.model_calls += 1
        self._touch(state, child)
        await checkpoint(state)
        self._trace_event(
            parent,
            "exploration.context.used",
            {
                "context_use_id": bundle.use_record.use_id,
                "snapshot_hash": bundle.use_record.snapshot_hash,
                "projection_hash": bundle.use_record.projection_hash,
                "included_item_count": len(bundle.use_record.included_item_ids),
                "omitted_item_count": len(bundle.use_record.omitted),
                "model_call": child.usage.model_calls,
            },
        )
        prompt = self._prompt_builder.build(bundle.projection)
        payload = {
            "context": prompt.payload,
            "turn_constraints": {
                "allowed_capability_ids": sorted(child.profile.allowed_capability_ids),
                "remaining_steps": child.profile.budget.max_steps - child.usage.steps,
                "remaining_action_calls": (
                    child.profile.budget.max_action_calls - child.usage.action_calls
                ),
                "remaining_observations": (
                    child.profile.budget.max_observations - len(child.observations)
                ),
            },
        }
        generation_request = GenerateRequest(
            model=child.profile.model_capability_id,
            messages=(
                ModelMessage(role=ModelRole.SYSTEM, content=_SYSTEM_PROMPT),
                *(
                    ModelMessage(role=ModelRole.SYSTEM, content=instruction)
                    for instruction in prompt.system_instructions
                ),
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
                name="exploration_turn_v1",
                schema=self._turn_schema,
                strictness=StructuredOutputStrictness.REQUIRED,
                on_unsupported=UnsupportedStructuredOutputBehavior.FAIL,
            ),
            temperature=0.0,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "purpose": "explore",
                "prompt_version": child.profile.prompt_version,
            },
        )
        result = await self._await_with_cancellation(
            self._generation.generate(
                generation_request,
                context,
                deadline_at=self._effective_deadline(plan, context),
                parent=parent,
                trace_enabled=trace_enabled,
            ),
            cancellation,
        )
        turn = self._parse_turn(result, child)
        catalog_hash = canonical_hash(
            [descriptor.model_dump(mode="json") for descriptor in catalog]
        )
        return turn, bundle.projection.projection_hash, catalog_hash

    def _parse_turn(
        self,
        result: GenerateResult,
        child: ExplorationState,
    ) -> CallCapabilityDraft | FinishDraft:
        if not isinstance(result, GenerateResult) or result.status is GenerateStatus.FAILED:
            cause_code = (
                result.error.code
                if isinstance(result, GenerateResult) and result.error is not None
                else "HARNESS.MODEL.INVALID_RESULT"
            )
            raise ExplorationError(
                "exploration model generation failed",
                code=ErrorCode.EXPLORATION_MODEL_FAILED,
                details={
                    "profile_id": child.profile.profile_id,
                    "model_capability_id": child.profile.model_capability_id,
                    "cause_code": cause_code,
                },
                retryable=(
                    result.error.retryable
                    if isinstance(result, GenerateResult) and result.error is not None
                    else False
                ),
            )
        if (
            result.output is None
            or result.output.type is not ModelResponseFormat.JSON
            or not isinstance(result.output.data, Mapping)
        ):
            raise ExplorationError(
                "exploration model turn must be a JSON object",
                code=ErrorCode.EXPLORATION_INVALID_TURN,
                details={"reason": "json_object_required"},
            )
        try:
            return self._turn_adapter.validate_python(dict(result.output.data))
        except (ValidationError, TypeError, ValueError) as exc:
            raise ExplorationError(
                "exploration model returned an invalid turn",
                code=ErrorCode.EXPLORATION_INVALID_TURN,
                details={"reason": type(exc).__name__},
            ) from exc

    def _validated_proposal(
        self,
        turn: CallCapabilityDraft,
        child: ExplorationState,
        *,
        projection_hash: str,
        catalog_hash: str,
    ) -> ActionProposal:
        proposal = ActionProposal(
            action_id=self._new_id(self._action_id_factory, "action_id"),
            exploration_id=child.exploration_id,
            step=child.usage.steps + 1,
            capability_id=turn.capability_id,
            input=turn.input,
            proposal_hash="0" * 64,
            catalog_snapshot_hash=catalog_hash,
            scope_hash=child.scope_hash,
            context_projection_hash=projection_hash,
            reason_code=turn.reason_code,
        )
        proposal = proposal.model_copy(update={"proposal_hash": action_proposal_hash(proposal)})
        self._action_executor.validate(proposal, child.profile)
        return proposal

    @staticmethod
    def _validate_finish(turn: FinishDraft, child: ExplorationState) -> None:
        evidence = {
            reference
            for observation in child.observations
            for reference in (observation.observation_id, *observation.evidence_refs)
        }
        missing = tuple(reference for reference in turn.evidence_refs if reference not in evidence)
        if missing:
            raise ExplorationError(
                "exploration finish references unknown evidence",
                code=ErrorCode.EXPLORATION_INVALID_TURN,
                details={"reason": "evidence_not_found", "missing_count": len(missing)},
            )

    async def _enforce_turn_budget(
        self,
        state: PlanExecutionState,
        child: ExplorationState,
        checkpoint: CheckpointCallback,
    ) -> ResultEnvelope | None:
        budget = child.profile.budget
        if child.usage.steps >= budget.max_steps:
            return await self._budget_terminal(
                state,
                child,
                ErrorCode.EXPLORATION_BUDGET_EXHAUSTED,
                "exploration step budget exhausted",
                checkpoint,
            )
        if child.usage.model_calls >= budget.max_model_calls:
            return await self._budget_terminal(
                state,
                child,
                ErrorCode.EXPLORATION_BUDGET_EXHAUSTED,
                "exploration model-call budget exhausted",
                checkpoint,
            )
        return None

    async def _budget_terminal(
        self,
        state: PlanExecutionState,
        child: ExplorationState,
        code: ErrorCode,
        message: str,
        checkpoint: CheckpointCallback,
    ) -> ResultEnvelope:
        error = ExplorationError(
            message,
            code=code,
            details={
                "profile_id": child.profile.profile_id,
                "steps": child.usage.steps,
                "model_calls": child.usage.model_calls,
                "action_calls": child.usage.action_calls,
            },
        )
        reliable = tuple(
            observation
            for observation in child.observations
            if observation.result_status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}
        )
        if reliable:
            output = ResultOutput(
                type="exploration_observations",
                data={
                    "observations": [
                        {
                            "observation_id": item.observation_id,
                            "summary": item.model_dump(mode="json")["bounded_summary"],
                            "evidence_refs": list(item.evidence_refs),
                        }
                        for item in reliable
                    ]
                },
            )
            result = ResultEnvelope.partial(
                output,
                (
                    *state.issues,
                    ResultIssue(source=child.node_id, error=error.to_detail()),
                ),
            )
            result = self._with_child_metadata(result, child)
        else:
            result = self._with_child_metadata(ResultEnvelope.failure(error.to_detail()), child)
        await self._terminalize(state, child, result, checkpoint)
        return result

    async def _terminal_failure(
        self,
        state: PlanExecutionState,
        child: ExplorationState,
        error: HarnessError,
        checkpoint: CheckpointCallback,
    ) -> ResultEnvelope:
        result = self._with_child_metadata(ResultEnvelope.failure(error.to_detail()), child)
        await self._terminalize(state, child, result, checkpoint)
        return result

    async def _terminal_cancelled(
        self,
        state: PlanExecutionState,
        child: ExplorationState,
        reason: str | None,
        checkpoint: CheckpointCallback,
    ) -> ResultEnvelope:
        result = ResultEnvelope.cancelled(
            metadata={
                "plan_id": child.plan_id,
                "exploration_id": child.exploration_id,
                "profile_id": child.profile.profile_id,
                **({"reason": reason} if reason else {}),
            }
        )
        await self._terminalize(state, child, result, checkpoint)
        return result

    async def _terminalize(
        self,
        state: PlanExecutionState,
        child: ExplorationState,
        result: ResultEnvelope,
        checkpoint: CheckpointCallback,
    ) -> None:
        now = self._now()
        child.status = {
            ResultStatus.SUCCESS: ExplorationStatus.SUCCEEDED,
            ResultStatus.PARTIAL: ExplorationStatus.PARTIAL,
            ResultStatus.FAILED: ExplorationStatus.FAILED,
            ResultStatus.DENIED: ExplorationStatus.DENIED,
            ResultStatus.CANCELLED: ExplorationStatus.CANCELLED,
        }[result.status]
        child.pending_action_id = None
        child.final_result = result
        child.updated_at = now
        child.completed_at = now
        outer = state.nodes[child.node_id]
        outer.status = {
            ResultStatus.SUCCESS: NodeExecutionStatus.SUCCEEDED,
            ResultStatus.PARTIAL: NodeExecutionStatus.SUCCEEDED,
            ResultStatus.FAILED: NodeExecutionStatus.FAILED,
            ResultStatus.DENIED: NodeExecutionStatus.DENIED,
            ResultStatus.CANCELLED: NodeExecutionStatus.CANCELLED,
        }[result.status]
        outer.result = result
        outer.error = result.error
        outer.completed_at = now
        state.status = {
            ResultStatus.SUCCESS: PlanExecutionStatus.SUCCEEDED,
            ResultStatus.PARTIAL: PlanExecutionStatus.PARTIAL,
            ResultStatus.FAILED: PlanExecutionStatus.FAILED,
            ResultStatus.DENIED: PlanExecutionStatus.DENIED,
            ResultStatus.CANCELLED: PlanExecutionStatus.CANCELLED,
        }[result.status]
        if result.status is ResultStatus.PARTIAL:
            for issue in result.issues:
                if issue not in state.issues:
                    state.issues.append(issue)
        elif result.error is not None:
            state.issues.append(ResultIssue(source=child.node_id, error=result.error))
        state.updated_at = now
        state.completed_at = now
        state.state_version += 1
        state.metadata["final_result"] = result.model_dump(mode="json")
        await checkpoint(state)

    def _observation(
        self,
        action: ActionExecutionState,
        result: ResultEnvelope,
    ) -> Observation:
        result_hash = result_envelope_hash(result)
        if result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}:
            summary: object = {
                "status": result.status.value,
                "output": (
                    result.output.model_dump(mode="json") if result.output is not None else None
                ),
                "issue_codes": [issue.error.code for issue in result.issues[:16]],
            }
        else:
            summary = {
                "status": result.status.value,
                "error_code": result.error.code if result.error is not None else None,
                "retryable": result.error.retryable if result.error is not None else False,
            }
        observation_id = self._new_id(self._observation_id_factory, "observation_id")
        return Observation(
            observation_id=observation_id,
            action_id=action.action_id,
            result_status=result.status,
            bounded_summary=_bounded_json(summary),
            evidence_refs=(observation_id, f"result:{result_hash}"),
            result_hash=result_hash,
        )

    @staticmethod
    def _complete_governed_action(
        action: ActionExecutionState,
        result: ResultEnvelope,
        status: str,
        *,
        error_code: str | None = None,
    ) -> None:
        action.status = status  # type: ignore[assignment]
        action.result = result
        action.error_code = error_code or (result.error.code if result.error is not None else None)
        action.completed_at = datetime.now(UTC)

    def _action_catalog(self, child: ExplorationState):
        allowed = child.profile.allowed_capability_ids
        return tuple(descriptor for descriptor in self._catalog.list() if descriptor.id in allowed)

    @staticmethod
    def _resolve_goal(request: Request, node: PlanNode) -> dict[str, object]:
        assert node.exploration is not None
        document = request.model_dump(mode="json")
        goal: dict[str, object] = {}
        for name, binding in node.exploration.goal_bindings.items():
            if isinstance(binding, LiteralBinding):
                goal[name] = binding.model_dump(mode="json")["value"]
                continue
            if isinstance(binding, RequestBinding):
                goal[name] = _resolve_pointer(document, binding.pointer)
                continue
            raise ExplorationError(
                "standalone exploration goal cannot depend on node output",
                code=ErrorCode.EXPLORATION_INVALID_PROFILE,
                details={"binding": name},
            )
        return goal

    def _require_plan(self, plan: ExecutionPlan) -> PlanNode:
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be ExecutionPlan")
        if (
            len(plan.nodes) != 1
            or plan.nodes[0].kind is not PlanNodeKind.EXPLORATION
            or plan.nodes[0].exploration is None
            or plan.edges
        ):
            raise ExplorationError(
                "minimal exploration requires the standalone one-node wrapper",
                code=ErrorCode.EXPLORATION_MODE_NOT_AVAILABLE,
                details={"plan_id": plan.plan_id},
            )
        return plan.nodes[0]

    @staticmethod
    def _record_for_validation(
        plan: ExecutionPlan,
        context: InvocationContext,
        state: PlanExecutionState,
    ):
        from harness_contracts import PlanExecutionRecord

        return PlanExecutionRecord(
            plan_id=plan.plan_id,
            plan=plan,
            context=context,
            state=state,
        )

    async def _await_with_cancellation(
        self,
        awaitable: Awaitable[object],
        cancellation: CancellationView,
    ):
        if cancellation.cancelled:
            raise _SignalCancelled
        task = asyncio.ensure_future(awaitable)
        waiter = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done and cancellation.cancelled:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise _SignalCancelled
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def _deadline_expired(self, plan: ExecutionPlan, context: InvocationContext) -> bool:
        deadline = self._effective_deadline(plan, context)
        return deadline is not None and self._now() >= deadline

    @staticmethod
    def _effective_deadline(
        plan: ExecutionPlan,
        context: InvocationContext,
    ) -> datetime | None:
        values = tuple(
            value for value in (context.deadline_at, plan.budget.deadline_at) if value is not None
        )
        return min(values) if values else None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _new_id(factory: IdFactory, field_name: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{field_name} factory must return a non-empty trimmed string")
        return value

    @staticmethod
    def _touch(state: PlanExecutionState, child: ExplorationState) -> None:
        now = datetime.now(UTC)
        child.updated_at = now
        state.updated_at = now
        state.state_version += 1

    @staticmethod
    def _with_metadata(
        result: ResultEnvelope,
        plan: ExecutionPlan,
        child: ExplorationState,
    ) -> ResultEnvelope:
        payload = result.model_dump(mode="json")
        metadata = dict(payload["metadata"])
        metadata.update(
            {
                "plan_id": plan.plan_id,
                "plan_revision": plan.revision,
                "exploration_id": child.exploration_id,
                "profile_id": child.profile.profile_id,
            }
        )
        return result.model_copy(update={"metadata": metadata})

    @staticmethod
    def _with_child_metadata(
        result: ResultEnvelope,
        child: ExplorationState,
    ) -> ResultEnvelope:
        payload = result.model_dump(mode="json")
        metadata = dict(payload["metadata"])
        metadata.update(
            {
                "plan_id": child.plan_id,
                "exploration_id": child.exploration_id,
                "profile_id": child.profile.profile_id,
            }
        )
        return result.model_copy(update={"metadata": metadata})

    def _trace_event(
        self,
        span: Span | None,
        name: str,
        attributes: dict[str, object],
    ) -> None:
        if span is None:
            return
        try:
            self._tracer.add_event(span, name, attributes=attributes)
        except Exception:
            return


def _bounded_json(value: object, *, depth: int = 0) -> object:
    if depth >= 6:
        return "[depth-limited]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= 2_048 else value[:2_048] + "…"
    if isinstance(value, Mapping):
        return {
            str(key)[:256]: _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, tuple | list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:32]]
    return f"[{type(value).__name__}]"


def _resolve_pointer(document: object, pointer: str) -> object:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ExplorationError(
            "exploration goal binding has an invalid JSON pointer",
            code=ErrorCode.EXPLORATION_INVALID_PROFILE,
        )
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
            continue
        if isinstance(current, tuple | list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
            continue
        raise ExplorationError(
            "exploration goal binding cannot be resolved",
            code=ErrorCode.EXPLORATION_INVALID_PROFILE,
            details={"pointer": pointer},
        )
    return current
