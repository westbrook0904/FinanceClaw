"""Registry、Policy、Trace 与 Capability SPI 之间的统一受控调用边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from harness_contracts import (
    ApprovalGrant,
    CapabilityError,
    Continuation,
    HarnessError,
    HarnessTimeoutError,
    InvocationContext,
    JsonValue,
    PolicyError,
    ProviderDescriptor,
    ProviderError,
    RegistryError,
    RequestError,
    RequestInput,
    ResultEnvelope,
    RetryPolicy,
    SelectionContext,
    TraceContext,
)
from harness_policy import PolicyContext, PolicyDecision, PolicyEffect, PolicyEngine, PolicyPhase
from harness_registry import CapabilityRegistry, ProviderRegistration, ResolvedCapability
from harness_selection import PrioritySelector, ProviderSelector
from harness_spi import AgentRequest, AgentSPI, ToolRequest, ToolSPI
from harness_trace import Span, SpanType, Tracer

from .lifecycle import InvocationLifecycle
from .provider_execution import (
    AttemptStartedCallback,
    ProviderExecutionCoordinator,
    SelectedProvider,
)

type InvocationParent = Span | TraceContext | None


class CapabilityInvoker:
    """执行一次不会绕过治理、追踪与错误归一化的 Capability 调用。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_engine: PolicyEngine,
        tracer: Tracer,
        *,
        lifecycle: InvocationLifecycle | None = None,
        provider_selector: ProviderSelector | None = None,
        provider_execution: ProviderExecutionCoordinator | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        effective_selector = provider_selector or (
            provider_execution.selector
            if isinstance(provider_execution, ProviderExecutionCoordinator)
            else PrioritySelector()
        )
        if not isinstance(effective_selector, ProviderSelector):
            raise TypeError("provider_selector must implement ProviderSelector")

        effective_provider_execution = provider_execution or ProviderExecutionCoordinator(
            effective_selector
        )
        if not isinstance(effective_provider_execution, ProviderExecutionCoordinator):
            raise TypeError("provider_execution must be ProviderExecutionCoordinator")
        if effective_provider_execution.selector is not effective_selector:
            raise ValueError("provider_execution and invoker must use the same selector")

        effective_lifecycle = lifecycle or InvocationLifecycle(tracer)
        if not isinstance(effective_lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if effective_lifecycle.tracer is not tracer:
            raise ValueError("lifecycle and invoker must use the same tracer")

        self._registry = registry
        self._policy_engine = policy_engine
        self._tracer = tracer
        self._provider_selector = effective_selector
        self._provider_execution = effective_provider_execution
        self._lifecycle = effective_lifecycle

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def provider_selector(self) -> ProviderSelector:
        return self._provider_selector

    @property
    def provider_execution(self) -> ProviderExecutionCoordinator:
        return self._provider_execution

    @property
    def lifecycle(self) -> InvocationLifecycle:
        return self._lifecycle

    async def invoke(
        self,
        capability_id: str,
        input: RequestInput,
        context: InvocationContext,
        *,
        plugin_id: str | None = None,
        timeout_ms: int | None = None,
        deadline_at: datetime | None = None,
        retry_policy: RetryPolicy | None = None,
        idempotency_key: str | None = None,
        retry_start_attempt: int = 1,
        attempt_started: AttemptStartedCallback | None = None,
        parent: InvocationParent = None,
        trace_enabled: bool = True,
    ) -> ResultEnvelope:
        """解析并调用一个能力，始终返回统一结果，调用方取消除外。"""

        self._validate_invocation(
            capability_id,
            input,
            context,
            plugin_id=plugin_id,
            timeout_ms=timeout_ms,
            deadline_at=deadline_at,
            retry_policy=retry_policy,
            idempotency_key=idempotency_key,
            retry_start_attempt=retry_start_attempt,
            attempt_started=attempt_started,
            parent=parent,
        )
        trace = _TraceAnchor(parent if parent is not None else context.trace_context)
        effective_deadline = self._effective_deadline(
            context,
            timeout_ms=timeout_ms,
            deadline_at=deadline_at,
        )
        effective_idempotency_key = idempotency_key
        if effective_idempotency_key is None:
            context_key = context.attributes.get("idempotency_key")
            if isinstance(context_key, str) and context_key.strip():
                effective_idempotency_key = context_key
        progress_error: Exception | None = None

        async def notify_attempt_started(attempt) -> None:
            nonlocal progress_error
            if attempt_started is None:
                return
            try:
                await attempt_started(attempt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                progress_error = exc
                raise

        try:
            resolution = self._resolve_capability(
                capability_id,
                context,
                plugin_id=plugin_id,
                deadline_at=effective_deadline,
                trace=trace,
                trace_enabled=trace_enabled,
            )

            async def invoke_selected(selected: SelectedProvider) -> ResultEnvelope:
                resolved = selected.resolved
                decision = self._evaluate_policy(
                    context,
                    resolved,
                    selected.registration.descriptor,
                    trace=trace,
                    trace_enabled=trace_enabled,
                )
                if decision.effect is PolicyEffect.DENY:
                    constraints = decision.model_dump(mode="json")["constraints"]
                    error = PolicyError(
                        decision.reason or "policy denied invocation",
                        details={"policy": decision.policy, "constraints": constraints},
                    )
                    return ResultEnvelope.denied(error.to_detail())
                if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                    return self._approval_required_result(
                        input,
                        context,
                        resolved,
                        decision,
                    )
                return await self._invoke_capability(
                    input,
                    context,
                    resolved,
                    timeout_ms=timeout_ms,
                    deadline_at=effective_deadline,
                    trace=trace,
                    trace_enabled=trace_enabled,
                )

            result = await self._provider_execution.execute(
                resolution.candidates,
                resolution.selection_context,
                invoke_selected,
                retry_policy=retry_policy,
                idempotency_key=effective_idempotency_key,
                deadline_at=effective_deadline,
                initial_selection=resolution.selected,
                retry_start_attempt=retry_start_attempt,
                attempt_started=(notify_attempt_started if attempt_started is not None else None),
            )
        except asyncio.CancelledError:
            raise
        except HarnessError as exc:
            if exc is progress_error:
                raise
            result = ResultEnvelope.failure(exc.to_detail())
        except Exception as exc:
            if exc is progress_error:
                raise
            wrapped = CapabilityError(
                "capability invocation failed",
                code="HARNESS.CAPABILITY.INVOCATION_FAILED",
                details={"capability_id": capability_id, "cause_type": type(exc).__name__},
            )
            result = ResultEnvelope.failure(wrapped.to_detail())

        return self._lifecycle.normalize_trace_id(
            result,
            trace.parent if trace_enabled else None,
        )

    def _validate_invocation(
        self,
        capability_id: str,
        input: RequestInput,
        context: InvocationContext,
        *,
        plugin_id: str | None,
        timeout_ms: int | None,
        deadline_at: datetime | None,
        retry_policy: RetryPolicy | None,
        idempotency_key: str | None,
        retry_start_attempt: int,
        attempt_started: AttemptStartedCallback | None,
        parent: InvocationParent,
    ) -> None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise TypeError("capability_id must be a non-empty string")
        if not isinstance(input, RequestInput):
            raise TypeError("input must be RequestInput")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")
        if plugin_id is not None and (not isinstance(plugin_id, str) or not plugin_id.strip()):
            raise TypeError("plugin_id must be a non-empty string when provided")
        if timeout_ms is not None and (
            not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0
        ):
            raise TypeError("timeout_ms must be a positive integer when provided")
        if deadline_at is not None and (
            not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() is None
        ):
            raise TypeError("deadline_at must be a timezone-aware datetime when provided")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be RetryPolicy when provided")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise TypeError("idempotency_key must be a non-empty string when provided")
        if (
            not isinstance(retry_start_attempt, int)
            or isinstance(retry_start_attempt, bool)
            or retry_start_attempt < 1
            or (retry_policy is not None and retry_start_attempt > retry_policy.max_attempts)
        ):
            raise ValueError("retry_start_attempt must be within RetryPolicy.max_attempts")
        if attempt_started is not None and not callable(attempt_started):
            raise TypeError("attempt_started must be callable when provided")
        if parent is not None and not isinstance(parent, Span | TraceContext):
            raise TypeError("parent must be Span, TraceContext, or None")

    @staticmethod
    def _effective_deadline(
        context: InvocationContext,
        *,
        timeout_ms: int | None,
        deadline_at: datetime | None,
    ) -> datetime | None:
        candidates = [item for item in (context.deadline_at, deadline_at) if item is not None]
        if timeout_ms is not None:
            candidates.append(datetime.now(UTC) + timedelta(milliseconds=timeout_ms))
        return min(candidates) if candidates else None

    def _resolve_capability(
        self,
        capability_id: str,
        context: InvocationContext,
        *,
        plugin_id: str | None,
        deadline_at: datetime | None,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> _ProviderResolution:
        span = (
            self._tracer.start_span(
                "registry.resolve",
                SpanType.REGISTRY_RESOLVE,
                parent=trace.parent,
                attributes=_compact_attributes(
                    {
                        "capability_id": capability_id,
                        "plugin_id": plugin_id,
                        "selector": self._provider_selector.name,
                    }
                ),
            )
            if trace_enabled
            else None
        )
        trace.capture(span)

        try:
            candidates = self._registry.candidates(
                capability_id,
                plugin_id=plugin_id,
            )
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(span)
            raise
        except RegistryError as exc:
            self._lifecycle.finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = RegistryError(
                "provider candidate discovery failed",
                code="HARNESS.REGISTRY.RESOLVE_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(span, wrapped)
            raise wrapped from exc

        if not candidates:
            error = RegistryError(
                "no capability matches query",
                details=_compact_attributes(
                    {
                        "capability_id": capability_id,
                        "plugin_id": plugin_id,
                    }
                ),
            )
            self._lifecycle.finish_error(span, error)
            raise error

        capability = candidates[0].capability

        trusted_tenant_id = context.tenant.tenant_id if context.tenant is not None else None
        identity_subject = context.identity.subject if context.identity is not None else None
        selection_context = SelectionContext(
            request_id=context.request.request_id,
            capability_id=capability_id,
            tenant_id=trusted_tenant_id,
            identity_subject=identity_subject,
            side_effect=capability.execution_profile.side_effect,
            egress=capability.execution_profile.egress,
            deadline_at=deadline_at or context.deadline_at,
        )

        try:
            selected = self._provider_execution.select(candidates, selection_context)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = ProviderError(
                "provider selection failed",
                code="HARNESS.PROVIDER.SELECTION_FAILED",
                details={
                    "capability_id": capability_id,
                    "selector": self._provider_selector.name,
                    "cause_type": type(exc).__name__,
                },
            )
            self._lifecycle.finish_error(span, wrapped)
            raise wrapped from exc

        registration = selected.registration
        selection = selected.decision
        resolved = selected.resolved
        self._lifecycle.finish_ok(
            span,
            attributes={
                "resolved_capability_id": resolved.descriptor.id,
                "plugin_id": resolved.plugin_id,
                "provider_id": registration.provider_id,
                "capability_type": resolved.descriptor.type.value,
                "candidate_count": len(candidates),
                "selection_key": selection.selection_key,
                "selection_reason": selection.reason_code,
                "selector": selection.selector,
            },
        )
        return _ProviderResolution(
            selected=selected,
            candidates=candidates,
            selection_context=selection_context,
        )

    def _evaluate_policy(
        self,
        context: InvocationContext,
        resolved: ResolvedCapability,
        provider_descriptor: ProviderDescriptor,
        *,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> PolicyDecision:
        span = (
            self._tracer.start_span(
                "policy.pre_execute",
                SpanType.POLICY,
                parent=trace.parent,
                attributes={
                    "capability_id": resolved.descriptor.id,
                    "provider_id": provider_descriptor.provider_id,
                },
            )
            if trace_enabled
            else None
        )
        policy_context = PolicyContext(
            invocation=(
                self._lifecycle.with_trace_context(context, span) if span is not None else context
            ),
            capability=resolved.descriptor,
            provider=provider_descriptor,
            phase=PolicyPhase.PRE_EXECUTE,
            approval_grant=self._approval_grant(context),
        )
        try:
            decision = self._policy_engine.evaluate(policy_context)
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(span)
            raise
        except PolicyError as exc:
            self._lifecycle.finish_error(span, exc)
            raise
        except Exception as exc:
            wrapped = PolicyError(
                "policy evaluation failed",
                code="HARNESS.POLICY.EVALUATION_FAILED",
                details={"cause_type": type(exc).__name__},
            )
            self._lifecycle.finish_error(span, wrapped)
            raise wrapped from exc

        attributes: dict[str, JsonValue] = {
            "effect": decision.effect.value,
            "policy": decision.policy,
            "provider_id": provider_descriptor.provider_id,
        }
        if policy_context.approval_grant is not None:
            attributes["approval_id"] = policy_context.approval_grant.approval_id
        self._lifecycle.finish_ok(span, attributes=attributes)
        return decision

    def _approval_required_result(
        self,
        input: RequestInput,
        context: InvocationContext,
        resolved: ResolvedCapability,
        decision: PolicyDecision,
    ) -> ResultEnvelope:
        """把 PRE_EXECUTE REQUIRE_APPROVAL 转成 Plan WAITING 或 Direct DENIED。"""

        plan_id = context.attributes.get("plan_id")
        node_id = context.attributes.get("node_id")
        if not isinstance(plan_id, str) or not isinstance(node_id, str):
            error = PolicyError(
                decision.reason or "human approval is required",
                code="HARNESS.POLICY.APPROVAL_REQUIRED",
                details={
                    "policy": decision.policy,
                    "capability_id": resolved.descriptor.id,
                },
            )
            return ResultEnvelope.denied(error.to_detail())

        content = input.model_dump(mode="json")["content"]
        parameter_names = sorted(content)[:32] if isinstance(content, dict) else []
        profile = resolved.descriptor.execution_profile
        continuation = Continuation(
            plan_id=plan_id,
            node_id=node_id,
            waiting_reason="policy_approval",
        )
        return ResultEnvelope.accepted(
            continuation,
            metadata={
                "approval_request": {
                    "capability": resolved.descriptor.id,
                    "side_effect": profile.side_effect.value,
                    "egress": profile.egress.value,
                    "parameter_summary": (
                        {"parameter_names": parameter_names} if parameter_names else {}
                    ),
                    "reason": decision.reason or "policy requires approval",
                    "policy": decision.policy,
                }
            },
        )

    @staticmethod
    def _approval_grant(context: InvocationContext) -> ApprovalGrant | None:
        """只接受 ExecutionEngine 从持久化状态注入的匹配 grant。"""

        payload = context.attributes.get("_harness_approval_grants")
        if payload is None:
            return None
        if not isinstance(payload, tuple | list):
            raise PolicyError(
                "approval grant context is invalid",
                code="HARNESS.POLICY.APPROVAL_GRANT_INVALID",
            )
        plan_id = context.attributes.get("plan_id")
        node_id = context.attributes.get("node_id")
        for raw in payload:
            try:
                grant = ApprovalGrant.model_validate(raw)
            except Exception as exc:
                raise PolicyError(
                    "approval grant context is invalid",
                    code="HARNESS.POLICY.APPROVAL_GRANT_INVALID",
                ) from exc
            if grant.plan_id == plan_id and grant.node_id == node_id:
                return grant
        return None

    async def _invoke_capability(
        self,
        input: RequestInput,
        context: InvocationContext,
        resolved: ResolvedCapability,
        *,
        timeout_ms: int | None,
        deadline_at: datetime | None,
        trace: _TraceAnchor,
        trace_enabled: bool,
    ) -> ResultEnvelope:
        capability_span = (
            self._tracer.start_span(
                f"capability.{resolved.descriptor.id}",
                SpanType.CAPABILITY,
                parent=trace.parent,
                attributes={
                    "capability_id": resolved.descriptor.id,
                    "plugin_id": resolved.plugin_id,
                    "provider_id": resolved.provider_id,
                },
            )
            if trace_enabled
            else None
        )
        provider = resolved.provider
        if isinstance(provider, AgentSPI):
            leaf_type = SpanType.AGENT
            leaf_name = f"agent.{resolved.descriptor.id}"
        elif isinstance(provider, ToolSPI):
            leaf_type = SpanType.TOOL
            leaf_name = f"tool.{resolved.descriptor.id}"
        else:
            error = CapabilityError(
                "resolved provider is neither AgentSPI nor ToolSPI",
                code="HARNESS.CAPABILITY.INVALID_PROVIDER",
                details={"capability_id": resolved.descriptor.id},
            )
            self._lifecycle.finish_error(capability_span, error)
            raise error

        try:
            self._validate_provider_type(resolved, provider)
        except CapabilityError as exc:
            self._lifecycle.finish_error(capability_span, exc)
            raise

        leaf_span = (
            self._tracer.start_span(
                leaf_name,
                leaf_type,
                parent=capability_span,
                attributes=_compact_attributes(
                    {
                        "capability_id": resolved.descriptor.id,
                        "provider_id": resolved.provider_id,
                    }
                ),
            )
            if trace_enabled
            else None
        )
        execution_context = (
            self._lifecycle.with_trace_context(context, leaf_span)
            if leaf_span is not None
            else context
        )
        try:
            result = await self._call_provider(
                input,
                execution_context,
                resolved,
                provider,
                timeout_ms=timeout_ms,
                deadline_at=deadline_at,
            )
        except asyncio.CancelledError:
            self._lifecycle.finish_cancelled(leaf_span)
            self._lifecycle.finish_cancelled(capability_span)
            raise
        except HarnessError as exc:
            self._lifecycle.finish_error(leaf_span, exc)
            self._lifecycle.finish_error(capability_span, exc)
            raise
        except Exception as exc:
            wrapped = CapabilityError(
                "capability execution failed",
                details={
                    "capability_id": resolved.descriptor.id,
                    "cause_type": type(exc).__name__,
                },
            )
            self._lifecycle.finish_error(leaf_span, wrapped)
            self._lifecycle.finish_error(capability_span, wrapped)
            raise wrapped from exc

        if not isinstance(result, ResultEnvelope):
            error = CapabilityError(
                "capability must return ResultEnvelope",
                code="HARNESS.CAPABILITY.INVALID_RESULT",
                details={"capability_id": resolved.descriptor.id},
            )
            self._lifecycle.finish_error(leaf_span, error)
            self._lifecycle.finish_error(capability_span, error)
            raise error

        self._lifecycle.finish_from_result(leaf_span, result)
        self._lifecycle.finish_from_result(capability_span, result)
        return result

    async def _call_provider(
        self,
        input: RequestInput,
        context: InvocationContext,
        resolved: ResolvedCapability,
        provider: AgentSPI | ToolSPI,
        *,
        timeout_ms: int | None,
        deadline_at: datetime | None,
    ) -> ResultEnvelope:
        async def execute() -> ResultEnvelope:
            if isinstance(provider, AgentSPI):
                return await provider.invoke(AgentRequest(input=input), context)

            payload = input.model_dump(mode="json")["content"]
            if not isinstance(payload, dict):
                raise RequestError(
                    "tool input content must be a JSON object",
                    details={"capability_id": resolved.descriptor.id},
                )
            return await provider.execute(ToolRequest(arguments=payload), context)

        effective_deadline = deadline_at
        if timeout_ms is not None:
            timeout_deadline = datetime.now(UTC) + timedelta(milliseconds=timeout_ms)
            effective_deadline = (
                timeout_deadline
                if effective_deadline is None
                else min(effective_deadline, timeout_deadline)
            )
        if effective_deadline is None:
            return await execute()
        remaining_seconds = (effective_deadline - datetime.now(UTC)).total_seconds()
        if remaining_seconds <= 0:
            raise HarnessTimeoutError(
                "capability deadline exceeded",
                details={
                    "capability_id": resolved.descriptor.id,
                    "deadline_at": effective_deadline.isoformat(),
                },
            )
        try:
            async with asyncio.timeout(remaining_seconds):
                return await execute()
        except TimeoutError as exc:
            raise HarnessTimeoutError(
                "capability execution timed out",
                details=_compact_attributes(
                    {
                        "capability_id": resolved.descriptor.id,
                        "timeout_ms": timeout_ms,
                        "deadline_at": effective_deadline.isoformat(),
                    }
                ),
            ) from exc

    def _validate_provider_type(
        self,
        resolved: ResolvedCapability,
        provider: AgentSPI | ToolSPI,
    ) -> None:
        expected = resolved.descriptor.type.value
        actual = "agent" if isinstance(provider, AgentSPI) else "tool"
        if expected != actual:
            raise CapabilityError(
                "provider type does not match capability descriptor",
                code="HARNESS.CAPABILITY.TYPE_MISMATCH",
                details={
                    "capability_id": resolved.descriptor.id,
                    "descriptor_type": expected,
                    "provider_type": actual,
                },
            )


def _compact_attributes(values: Mapping[str, JsonValue | None]) -> dict[str, JsonValue]:
    return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class _ProviderResolution:
    selected: SelectedProvider
    candidates: tuple[ProviderRegistration, ...]
    selection_context: SelectionContext


@dataclass(slots=True)
class _TraceAnchor:
    """让没有上级 Span 的独立 Invoker 调用仍只产生一个 trace。"""

    parent: InvocationParent

    def capture(self, span: Span | None) -> None:
        if self.parent is None and span is not None:
            self.parent = TraceContext(trace_id=span.trace_id)
