"""Minimal Explore Action 的静态 scope/schema 守卫与统一调用入口。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from harness_contracts import (
    ActionProposal,
    CapabilityCompletionMode,
    CapabilityType,
    EgressType,
    ErrorCode,
    ExplorationError,
    ExplorationProfileSnapshot,
    InvocationContext,
    ResultEnvelope,
    RetryPolicy,
    SideEffectType,
)
from harness_registry import CapabilityCatalog
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_trace import Span, SpanType, Tracer
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class ScopedActionExecutor:
    """在任何 outbound 前重新校验 Proposal，并只通过 CapabilityInvoker 执行。"""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        invoker: CapabilityInvoker,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
    ) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must implement CapabilityCatalog")
        if not isinstance(invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if invoker.tracer is not tracer or invoker.lifecycle is not lifecycle:
            raise ValueError("action executor components must share tracer and lifecycle")
        self._catalog = catalog
        self._invoker = invoker
        self._tracer = tracer
        self._lifecycle = lifecycle

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    @property
    def invoker(self) -> CapabilityInvoker:
        return self._invoker

    async def execute(
        self,
        proposal: ActionProposal,
        profile: ExplorationProfileSnapshot,
        context: InvocationContext,
        *,
        deadline_at: datetime | None,
        parent: Span | None,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        self.validate(proposal, profile)
        action_span = (
            self._tracer.start_span(
                f"action.{proposal.action_id}",
                SpanType.ACTION,
                parent=parent,
                attributes={
                    "action_id": proposal.action_id,
                    "exploration_id": proposal.exploration_id,
                    "step": proposal.step,
                    "capability_id": proposal.capability_id,
                    "proposal_hash": proposal.proposal_hash,
                    "scope_hash": proposal.scope_hash,
                    "context_projection_hash": proposal.context_projection_hash,
                },
            )
            if trace_enabled
            else None
        )
        invocation = (
            self._lifecycle.with_trace_context(context, action_span)
            if action_span is not None
            else context
        )
        try:
            result = await self._invoker.invoke(
                proposal.capability_id,
                proposal.input,
                invocation,
                deadline_at=deadline_at,
                retry_policy=RetryPolicy(max_attempts=1),
                parent=action_span or parent,
                trace_enabled=trace_enabled,
            )
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(action_span)
            raise
        except Exception as exc:
            self._lifecycle.finish_error(action_span, exc)
            raise
        self._lifecycle.finish_from_result(action_span, result)
        return result

    def validate(
        self,
        proposal: ActionProposal,
        profile: ExplorationProfileSnapshot,
    ) -> None:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(profile, ExplorationProfileSnapshot):
            raise TypeError("profile must be ExplorationProfileSnapshot")
        capability_id = proposal.capability_id
        if capability_id not in profile.allowed_capability_ids:
            self._invalid(capability_id, "outside_profile_scope")
        descriptor = self._catalog.get(capability_id)
        if descriptor is None:
            self._invalid(capability_id, "capability_not_found")
        assert descriptor is not None
        if descriptor.type not in {CapabilityType.AGENT, CapabilityType.TOOL}:
            self._invalid(capability_id, "capability_type")
        execution = descriptor.execution_profile
        if execution.side_effect not in {SideEffectType.NONE, SideEffectType.READ}:
            self._invalid(capability_id, "side_effect")
        if execution.egress not in {EgressType.NONE, EgressType.INTERNAL}:
            self._invalid(capability_id, "egress")
        if execution.completion_mode is not CapabilityCompletionMode.SYNC:
            self._invalid(capability_id, "completion_mode")
        try:
            validator = Draft202012Validator(dict(descriptor.input_schema))
            input_document = proposal.input.model_dump(mode="json")["content"]
            errors = tuple(validator.iter_errors(input_document))
        except SchemaError as exc:
            raise ExplorationError(
                "exploration capability input schema is invalid",
                code=ErrorCode.EXPLORATION_ACTION_INVALID,
                details={"capability_id": capability_id, "reason": "invalid_input_schema"},
            ) from exc
        if errors:
            self._invalid(capability_id, "input_schema", issue_count=len(errors))

    @staticmethod
    def _invalid(capability_id: str, reason: str, **details: int) -> None:
        raise ExplorationError(
            "exploration action is outside the executable scope",
            code=ErrorCode.EXPLORATION_ACTION_INVALID,
            details={"capability_id": capability_id, "reason": reason, **details},
        )
