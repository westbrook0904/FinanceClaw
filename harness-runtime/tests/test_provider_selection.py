"""Stage 3A CapabilityInvoker Provider Selection 集成测试。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityExecutionProfile,
    CapabilityType,
    ErrorCategory,
    IdempotencyType,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealthStatus,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RetryPolicy,
    SelectionContext,
    SelectionDecision,
    SideEffectType,
)
from harness_events import ExecutionEventName, InMemoryEventBus
from harness_policy import AllowAllPolicy, Policy, PolicyContext, PolicyDecision, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, ProviderRegistration
from harness_runtime import CapabilityInvoker
from harness_selection import (
    EligibilityPipeline,
    PrioritySelector,
    ProviderSelector,
    StaticHealthSource,
)
from harness_spi import AgentRequest, AgentSPI
from harness_trace import InMemoryTracer, SpanStatus, SpanType

CAPABILITY_ID = "web.search/v1"


class RecordingSearchAgent(AgentSPI):
    def __init__(
        self,
        provider_name: str,
        *,
        execution_profile: CapabilityExecutionProfile | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.calls = 0
        self._descriptor = CapabilityDescriptor(
            id=CAPABILITY_ID,
            name="Web Search",
            type=CapabilityType.AGENT,
            version="1.0.0",
            execution_profile=execution_profile or CapabilityExecutionProfile(),
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data={"provider": self.provider_name},
            )
        )


class ScriptedSearchAgent(RecordingSearchAgent):
    def __init__(
        self,
        provider_name: str,
        results: Sequence[ResultEnvelope],
        *,
        execution_profile: CapabilityExecutionProfile | None = None,
    ) -> None:
        super().__init__(provider_name, execution_profile=execution_profile)
        self._results = tuple(results)

    async def invoke(self, request: AgentRequest, context: InvocationContext) -> ResultEnvelope:
        self.calls += 1
        index = min(self.calls - 1, len(self._results) - 1)
        return self._results[index]


class ProviderAwareDenyPolicy(Policy):
    def __init__(self, denied_provider_id: str) -> None:
        self.denied_provider_id = denied_provider_id
        self.seen_provider_id: str | None = None

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        self.seen_provider_id = (
            context.provider.provider_id if context.provider is not None else None
        )
        if self.seen_provider_id == self.denied_provider_id:
            return PolicyDecision.deny(
                self.name,
                reason="provider denied by test policy",
            )
        return PolicyDecision.allow(self.name, reason="provider allowed")


class InvalidSelector(ProviderSelector):
    @property
    def name(self) -> str:
        return "invalid-selector"

    def select(
        self,
        candidates,
        context: SelectionContext,
    ) -> SelectionDecision:
        return SelectionDecision(
            capability_id=context.capability_id,
            selected_provider_id="outside-candidate-set",
            eligible_candidates=("outside-candidate-set",),
            selector=self.name,
            reason_code="TEST_INVALID",
            selection_key="invalid-selection-key",
        )


class CountingSelector(ProviderSelector):
    def __init__(self) -> None:
        self.calls = 0
        self._delegate = PrioritySelector()

    @property
    def name(self) -> str:
        return "counting-priority"

    def select(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
    ) -> SelectionDecision:
        self.calls += 1
        return self._delegate.select(candidates, context).model_copy(update={"selector": self.name})


def register(
    registry: InMemoryCapabilityRegistry,
    agent: AgentSPI,
    *,
    provider_id: str,
    plugin_id: str,
    priority: int,
    equivalence_group: str | None = None,
) -> None:
    registry.register_provider(
        agent,
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=CAPABILITY_ID,
            plugin_id=plugin_id,
            implementation_version="1.0.0",
            priority=priority,
            equivalence_group=equivalence_group,
        ),
    )


def failure(
    code: str,
    *,
    retryable: bool = False,
    fallbackable: bool = False,
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "injected provider failure",
            code=code,
            retryable=retryable,
            fallbackable=fallbackable,
        ).to_detail()
    )


def make_context() -> InvocationContext:
    request = Request(
        request_id="req-selection-001",
        input=RequestInput(type="json", content={"query": "FinanceClaw"}),
    )
    return InvocationContext(request=request)


def make_invoker(
    registry: InMemoryCapabilityRegistry,
    *,
    selector: ProviderSelector | None = None,
    policy_engine: PolicyEngine | None = None,
    event_publisher: InMemoryEventBus | None = None,
) -> tuple[CapabilityInvoker, InMemoryTracer]:
    tracer = InMemoryTracer()
    invoker = CapabilityInvoker(
        registry,
        policy_engine or PolicyEngine((AllowAllPolicy(),)),
        tracer,
        provider_selector=selector,
        event_publisher=event_publisher,
    )
    return invoker, tracer


class CapabilityInvokerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_selector_executes_only_selected_provider(self) -> None:
        registry = InMemoryCapabilityRegistry()
        google = RecordingSearchAgent("google")
        baidu = RecordingSearchAgent("baidu")
        register(
            registry,
            google,
            provider_id="google-search",
            plugin_id="google-plugin",
            priority=50,
        )
        register(
            registry,
            baidu,
            provider_id="baidu-search",
            plugin_id="baidu-plugin",
            priority=100,
        )
        invoker, tracer = make_invoker(registry)

        context = make_context()
        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "baidu")
        self.assertEqual(google.calls, 0)
        self.assertEqual(baidu.calls, 1)
        resolve_span = next(
            span for span in tracer.spans() if span.type is SpanType.REGISTRY_RESOLVE
        )
        self.assertEqual(resolve_span.attributes["provider_id"], "baidu-search")
        self.assertEqual(resolve_span.attributes["candidate_count"], 2)

    async def test_health_eligibility_can_select_backup_provider(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = RecordingSearchAgent("primary")
        backup = RecordingSearchAgent("backup")
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        selector = PrioritySelector(
            EligibilityPipeline(
                StaticHealthSource(
                    {
                        "search-primary": ProviderHealthStatus.UNHEALTHY,
                        "search-backup": ProviderHealthStatus.HEALTHY,
                    }
                )
            )
        )
        events = InMemoryEventBus()
        invoker, _ = make_invoker(
            registry,
            selector=selector,
            event_publisher=events,
        )
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "backup")
        self.assertEqual(primary.calls, 0)
        self.assertEqual(backup.calls, 1)
        candidates = next(
            event
            for event in events.events()
            if event.name is ExecutionEventName.PROVIDER_CANDIDATES
        )
        self.assertEqual(
            candidates.model_dump(mode="json")["attributes"]["rejected_candidates"],
            [{"provider_id": "search-primary", "reason_code": "UNHEALTHY"}],
        )

    async def test_plugin_constraint_limits_candidate_set_before_selection(self) -> None:
        registry = InMemoryCapabilityRegistry()
        google = RecordingSearchAgent("google")
        baidu = RecordingSearchAgent("baidu")
        register(
            registry,
            google,
            provider_id="google-search",
            plugin_id="google-plugin",
            priority=100,
        )
        register(
            registry,
            baidu,
            provider_id="baidu-search",
            plugin_id="baidu-plugin",
            priority=1,
        )
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            plugin_id="baidu-plugin",
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "baidu")
        self.assertEqual(google.calls, 0)
        self.assertEqual(baidu.calls, 1)

    async def test_pre_execute_policy_receives_selected_provider(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = RecordingSearchAgent("primary")
        backup = RecordingSearchAgent("backup")
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        policy = ProviderAwareDenyPolicy("search-primary")
        invoker, _ = make_invoker(
            registry,
            policy_engine=PolicyEngine((policy,)),
        )
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(policy.seen_provider_id, "search-primary")
        self.assertEqual(primary.calls, 0)
        self.assertEqual(backup.calls, 0)

    async def test_multi_provider_write_invokes_primary_when_fallback_is_not_needed(
        self,
    ) -> None:
        registry = InMemoryCapabilityRegistry()
        write_profile = CapabilityExecutionProfile(side_effect=SideEffectType.WRITE)
        primary = RecordingSearchAgent("primary", execution_profile=write_profile)
        backup = RecordingSearchAgent("backup", execution_profile=write_profile)
        register(
            registry,
            primary,
            provider_id="write-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="write-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        invoker, _ = make_invoker(registry)
        request = Request(
            request_id="req-write-plan",
            input=RequestInput(type="json", content={"query": "write"}),
        )
        context = InvocationContext(
            request=request,
            attributes={"plan_id": "plan-001", "node_id": "node-001"},
        )

        result = await invoker.invoke(CAPABILITY_ID, request.input, context)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 0)

    async def test_retry_stays_on_selected_provider_then_succeeds(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = ScriptedSearchAgent(
            "primary",
            (
                failure("TEST.TRANSIENT", retryable=True, fallbackable=True),
                ResultEnvelope.success(ResultOutput(type="json", data={"provider": "primary"})),
            ),
        )
        backup = RecordingSearchAgent("backup")
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        selector = CountingSelector()
        invoker, _ = make_invoker(registry, selector=selector)
        context = make_context()

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 0)
        self.assertEqual(selector.calls, 1)

    async def test_retry_exhaustion_reselects_backup_and_resets_retry_attempt(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.PRIMARY", retryable=True, fallbackable=True),),
        )
        backup = RecordingSearchAgent("backup")
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        selector = CountingSelector()
        invoker, _ = make_invoker(registry, selector=selector)
        context = make_context()
        attempts = []

        async def record_attempt(attempt) -> None:
            attempts.append(attempt)

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
            attempt_started=record_attempt,
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(primary.calls, 2)
        self.assertEqual(backup.calls, 1)
        self.assertEqual(selector.calls, 2)
        self.assertEqual(
            [(item.provider_id, item.provider_attempt, item.retry_attempt) for item in attempts],
            [
                ("search-primary", 1, 1),
                ("search-primary", 1, 2),
                ("search-backup", 2, 1),
            ],
        )

    async def test_retry_and_fallback_emit_explainable_provider_observability(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.PRIMARY", retryable=True, fallbackable=True),),
        )
        backup = RecordingSearchAgent("backup")
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        events = InMemoryEventBus()
        invoker, tracer = make_invoker(registry, event_publisher=events)
        context = make_context()
        parent = tracer.start_span("runtime.test", SpanType.RUNTIME)

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
            parent=parent,
        )
        tracer.end_span(parent)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(
            [event.name for event in events.events()],
            [
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_SELECTED,
                ExecutionEventName.PROVIDER_FAILED,
                ExecutionEventName.PROVIDER_RETRYING,
                ExecutionEventName.PROVIDER_FAILED,
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_FALLBACK,
                ExecutionEventName.PROVIDER_SELECTED,
            ],
        )
        self.assertTrue(
            all(event.request_id == context.request.request_id for event in events.events())
        )
        selection_spans = tuple(
            span for span in tracer.spans() if span.type is SpanType.PROVIDER_SELECT
        )
        self.assertEqual(
            [span.attributes["provider_id"] for span in selection_spans],
            ["search-primary", "search-backup"],
        )
        self.assertEqual(
            [span.attributes["provider_attempt"] for span in selection_spans],
            [1, 2],
        )
        fallback_trace = next(
            event for event in tracer.events() if event.name == "provider.fallback"
        )
        self.assertEqual(fallback_trace.attributes["source_provider_id"], "search-primary")
        self.assertEqual(fallback_trace.attributes["target_provider_id"], "search-backup")

    async def test_all_fallback_providers_failed_preserves_last_failure(self) -> None:
        registry = InMemoryCapabilityRegistry()
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.PRIMARY", fallbackable=True),),
        )
        backup = ScriptedSearchAgent(
            "backup",
            (failure("TEST.BACKUP", fallbackable=True),),
        )
        register(
            registry,
            primary,
            provider_id="search-primary",
            plugin_id="primary-plugin",
            priority=100,
        )
        register(
            registry,
            backup,
            provider_id="search-backup",
            plugin_id="backup-plugin",
            priority=10,
        )
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "TEST.BACKUP")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)

    async def test_non_idempotent_write_fallback_is_fail_closed(self) -> None:
        registry = InMemoryCapabilityRegistry()
        write_profile = CapabilityExecutionProfile(side_effect=SideEffectType.WRITE)
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.WRITE", fallbackable=True),),
            execution_profile=write_profile,
        )
        backup = RecordingSearchAgent("backup", execution_profile=write_profile)
        register(
            registry,
            primary,
            provider_id="write-primary",
            plugin_id="primary-plugin",
            priority=100,
            equivalence_group="payments",
        )
        register(
            registry,
            backup,
            provider_id="write-backup",
            plugin_id="backup-plugin",
            priority=10,
            equivalence_group="payments",
        )
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.FALLBACK_UNSAFE")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 0)

    async def test_idempotent_write_fallback_requires_matching_equivalence_group(
        self,
    ) -> None:
        registry = InMemoryCapabilityRegistry()
        write_profile = CapabilityExecutionProfile(
            side_effect=SideEffectType.WRITE,
            idempotency=IdempotencyType.REQUIRED,
        )
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.WRITE", fallbackable=True),),
            execution_profile=write_profile,
        )
        mismatched = RecordingSearchAgent("mismatched", execution_profile=write_profile)
        register(
            registry,
            primary,
            provider_id="write-primary",
            plugin_id="primary-plugin",
            priority=100,
            equivalence_group="payments-prod",
        )
        register(
            registry,
            mismatched,
            provider_id="write-mismatched",
            plugin_id="backup-plugin",
            priority=10,
            equivalence_group="payments-backup",
        )
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            idempotency_key="payment-42",
        )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.FALLBACK_UNSAFE")
        self.assertEqual(mismatched.calls, 0)

    async def test_idempotent_write_same_group_with_stable_key_can_fallback(self) -> None:
        registry = InMemoryCapabilityRegistry()
        write_profile = CapabilityExecutionProfile(
            side_effect=SideEffectType.WRITE,
            idempotency=IdempotencyType.REQUIRED,
        )
        primary = ScriptedSearchAgent(
            "primary",
            (failure("TEST.WRITE", fallbackable=True),),
            execution_profile=write_profile,
        )
        backup = RecordingSearchAgent("backup", execution_profile=write_profile)
        register(
            registry,
            primary,
            provider_id="write-primary",
            plugin_id="primary-plugin",
            priority=100,
            equivalence_group="payments-prod",
        )
        register(
            registry,
            backup,
            provider_id="write-backup",
            plugin_id="backup-plugin",
            priority=10,
            equivalence_group="payments-prod",
        )
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(
            CAPABILITY_ID,
            context.request.input,
            context,
            idempotency_key="payment-42",
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "backup")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)

    async def test_registry_miss_remains_registry_failure(self) -> None:
        registry = InMemoryCapabilityRegistry()
        invoker, _ = make_invoker(registry)
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.REGISTRY)

    async def test_invalid_selector_decision_is_fail_closed(self) -> None:
        registry = InMemoryCapabilityRegistry()
        provider = RecordingSearchAgent("google")
        register(
            registry,
            provider,
            provider_id="google-search",
            plugin_id="google-plugin",
            priority=10,
        )
        invoker, tracer = make_invoker(registry, selector=InvalidSelector())
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.PROVIDER)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.SELECTION_FAILED")
        self.assertEqual(provider.calls, 0)
        selection_span = next(
            span for span in tracer.spans() if span.type is SpanType.PROVIDER_SELECT
        )
        self.assertEqual(selection_span.status, SpanStatus.ERROR)
        self.assertEqual(selection_span.error.code, "HARNESS.PROVIDER.SELECTION_FAILED")


if __name__ == "__main__":
    unittest.main()
