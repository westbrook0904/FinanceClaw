"""Stage 3A CapabilityInvoker Provider Selection 集成测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    ErrorCategory,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealthStatus,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    SelectionContext,
    SelectionDecision,
    SideEffectType,
)
from harness_policy import AllowAllPolicy, Policy, PolicyContext, PolicyDecision, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry
from harness_runtime import CapabilityInvoker
from harness_selection import (
    EligibilityPipeline,
    PrioritySelector,
    ProviderSelector,
    StaticHealthSource,
)
from harness_spi import AgentRequest, AgentSPI
from harness_trace import InMemoryTracer, SpanType


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


def register(
    registry: InMemoryCapabilityRegistry,
    agent: RecordingSearchAgent,
    *,
    provider_id: str,
    plugin_id: str,
    priority: int,
) -> None:
    registry.register_provider(
        agent,
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=CAPABILITY_ID,
            plugin_id=plugin_id,
            implementation_version="1.0.0",
            priority=priority,
        ),
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
) -> tuple[CapabilityInvoker, InMemoryTracer]:
    tracer = InMemoryTracer()
    invoker = CapabilityInvoker(
        registry,
        policy_engine or PolicyEngine((AllowAllPolicy(),)),
        tracer,
        provider_selector=selector,
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
        invoker, _ = make_invoker(registry, selector=selector)
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "backup")
        self.assertEqual(primary.calls, 0)
        self.assertEqual(backup.calls, 1)

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

    async def test_multi_provider_write_plan_is_fail_closed_before_checkpoint_support(
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

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.RESUME_UNSAFE")
        self.assertEqual(primary.calls, 0)
        self.assertEqual(backup.calls, 0)

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
        invoker, _ = make_invoker(registry, selector=InvalidSelector())
        context = make_context()

        result = await invoker.invoke(CAPABILITY_ID, context.request.input, context)

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.PROVIDER)
        self.assertEqual(result.error.code, "HARNESS.PROVIDER.SELECTION_FAILED")
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
