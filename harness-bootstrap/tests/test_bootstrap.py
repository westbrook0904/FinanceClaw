"""FinanceClaw 核心 Composition Root 测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import (
    BootstrapState,
    BootstrapStateError,
    HarnessApplication,
    build_harness,
)
from harness_context import ContextPipeline, ContextPolicy
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    PluginError,
    Request,
    RequestInput,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
)
from harness_events import ExecutionEventName, InMemoryEventBus
from harness_plugin_local import LocalPluginProvider
from harness_policy import AllowAllPolicy, Policy, PolicyContext, PolicyDecision, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_trace import InMemoryTracer


class EchoTool(ToolSPI):
    def __init__(self, capability_id: str = "echo.tool/v1", *, version: str = "1.0.0") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version=version,
        )
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(self, request: ToolRequest, context: InvocationContext) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope.success(
            ResultOutput(type="json", data=request.model_dump(mode="json")["arguments"])
        )


class StubPlugin(PluginSPI):
    def __init__(self, plugin_id: str, providers: tuple[ToolSPI, ...]) -> None:
        self.plugin_id = plugin_id
        self.providers = providers
        self.initialize_count = 0
        self.shutdown_count = 0

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id=self.plugin_id,
            name=self.plugin_id,
            version="1.0.0",
            sdk_version="1",
            capabilities=tuple(provider.descriptor().id for provider in self.providers),
        )

    def capabilities(self) -> tuple[ToolSPI, ...]:
        return self.providers

    async def initialize(self) -> None:
        self.initialize_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


class DenyAllPolicy(Policy):
    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.deny(self.name, reason="blocked by bootstrap test")


def make_request(capability_id: str = "echo.tool/v1") -> Request:
    return Request(
        input=RequestInput(type="json", content={"message": "hello"}),
        target=RequestTarget(capability=capability_id),
    )


class BootstrapFactoryTests(unittest.TestCase):
    def test_default_build_contains_only_domain_core(self) -> None:
        app = build_harness(entry_point_group=None)

        self.assertIsInstance(app, HarnessApplication)
        self.assertEqual(app.state, BootstrapState.CREATED)
        self.assertIsInstance(app.registry, InMemoryCapabilityRegistry)
        self.assertIsInstance(app.tracer, InMemoryTracer)
        self.assertIsInstance(app.components.context_factory, DefaultInvocationContextFactory)
        self.assertIsInstance(app.invoker, CapabilityInvoker)
        self.assertIs(app.runtime.invoker, app.invoker)
        self.assertIs(app.runtime.lifecycle, app.components.lifecycle)
        self.assertIsInstance(app.capability_catalog, RegistryCapabilityCatalog)
        self.assertIsInstance(app.context_pipeline, ContextPipeline)
        self.assertIs(app.context_pipeline.policy.policy_engine, app.policy_engine)
        self.assertEqual(len(app.policy_engine.policies), 1)
        self.assertIsInstance(app.policy_engine.policies[0], AllowAllPolicy)
        for removed in (
            "model_gateway",
            "router",
            "planner_registry",
            "execution_engine",
            "state_store",
        ):
            self.assertFalse(hasattr(app, removed))

    def test_custom_core_components_are_wired_by_identity(self) -> None:
        registry = InMemoryCapabilityRegistry()
        policy_engine = PolicyEngine((AllowAllPolicy(),))
        tracer = InMemoryTracer()
        context_factory = DefaultInvocationContextFactory()
        catalog = RegistryCapabilityCatalog(registry)

        app = build_harness(
            registry=registry,
            policy_engine=policy_engine,
            tracer=tracer,
            context_factory=context_factory,
            capability_catalog=catalog,
            entry_point_group=None,
        )

        self.assertIs(app.registry, registry)
        self.assertIs(app.policy_engine, policy_engine)
        self.assertIs(app.tracer, tracer)
        self.assertIs(app.components.context_factory, context_factory)
        self.assertIs(app.capability_catalog, catalog)

    def test_rejects_ambiguous_or_mismatched_composition(self) -> None:
        with self.assertRaises(ValueError):
            build_harness(
                policies=(AllowAllPolicy(),),
                policy_engine=PolicyEngine(),
                entry_point_group=None,
            )
        with self.assertRaises(ValueError):
            build_harness(
                policy_engine=PolicyEngine((AllowAllPolicy(),)),
                context_pipeline=ContextPipeline(ContextPolicy(PolicyEngine())),
                entry_point_group=None,
            )
        with self.assertRaises(ValueError):
            build_harness(
                plugins=(StubPlugin("echo", (EchoTool(),)),),
                plugin_provider=LocalPluginProvider(entry_point_group=None),
                entry_point_group=None,
            )


class BootstrapLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_loads_plugin_and_direct_runtime_invokes_it(self) -> None:
        tool = EchoTool()
        plugin = StubPlugin("echo-plugin", (tool,))
        app = build_harness(plugins=(plugin,), entry_point_group=None)

        loaded = await app.start()
        result = await app.invoke(make_request())

        self.assertEqual(app.state, BootstrapState.STARTED)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(tool.calls, 1)
        self.assertIsInstance(app.event_publisher, InMemoryEventBus)
        self.assertEqual(
            [event.name for event in app.event_publisher.events()],
            [
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_SELECTED,
            ],
        )

        await app.shutdown()
        self.assertEqual(app.state, BootstrapState.STOPPED)
        self.assertEqual(plugin.shutdown_count, 1)
        self.assertIsNone(app.registry.get("echo.tool/v1"))

    async def test_lifecycle_is_idempotent_and_stopped_app_cannot_restart(self) -> None:
        plugin = StubPlugin("echo-plugin", (EchoTool(),))
        app = build_harness(plugins=(plugin,), entry_point_group=None)
        first = await app.start()
        second = await app.start()
        await app.shutdown()
        await app.shutdown()

        self.assertEqual(first, second)
        self.assertEqual(plugin.initialize_count, 1)
        self.assertEqual(plugin.shutdown_count, 1)
        with self.assertRaises(BootstrapStateError):
            await app.start()
        with self.assertRaises(BootstrapStateError):
            await app.invoke(make_request())

    async def test_startup_failure_rolls_back_batch(self) -> None:
        first = StubPlugin("first", (EchoTool("shared.tool/v1"),))
        second = StubPlugin("second", (EchoTool("shared.tool/v1", version="2.0.0"),))
        app = build_harness(plugins=(first, second), entry_point_group=None)

        with self.assertRaises(PluginError):
            await app.start()

        self.assertEqual(app.state, BootstrapState.CREATED)
        self.assertEqual(app.registry.list(), ())
        self.assertEqual(first.shutdown_count, 1)
        self.assertEqual(second.shutdown_count, 1)

    async def test_injected_policy_controls_runtime(self) -> None:
        tool = EchoTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            policies=(DenyAllPolicy(),),
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.invoke(make_request())
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(tool.calls, 0)


if __name__ == "__main__":
    unittest.main()
