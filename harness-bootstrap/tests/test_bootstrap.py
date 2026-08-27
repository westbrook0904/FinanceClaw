"""harness-bootstrap 阶段一 Composition Root 测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import (
    BootstrapState,
    BootstrapStateError,
    HarnessApplication,
    build_harness,
)
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionPlan,
    InvocationContext,
    LiteralBinding,
    NodeOutputBinding,
    PlanNode,
    PluginError,
    Request,
    RequestInput,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
)
from harness_events import ExecutionEventName, InMemoryEventBus
from harness_model import ModelGateway
from harness_planning import PlanValidator
from harness_plugin_local import LocalPluginProvider
from harness_policy import AllowAllPolicy, Policy, PolicyContext, PolicyDecision, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_state import InMemoryStateStore
from harness_trace import InMemoryTracer


class EchoTool(ToolSPI):
    def __init__(
        self,
        capability_id: str = "echo.tool/v1",
        *,
        version: str = "1.0.0",
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version=version,
        )
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data=request.model_dump(mode="json")["arguments"],
            )
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
    def test_default_build_has_no_startup_side_effects(self) -> None:
        app = build_harness(entry_point_group=None)

        self.assertIsInstance(app, HarnessApplication)
        self.assertEqual(app.state, BootstrapState.CREATED)
        self.assertIsInstance(app.registry, InMemoryCapabilityRegistry)
        self.assertIsInstance(app.tracer, InMemoryTracer)
        self.assertIsInstance(
            app.components.context_factory,
            DefaultInvocationContextFactory,
        )
        self.assertIsInstance(app.invoker, CapabilityInvoker)
        self.assertIsInstance(app.model_gateway, ModelGateway)
        self.assertIs(app.model_gateway.registry, app.registry)
        self.assertIs(app.model_gateway.tracer, app.tracer)
        self.assertIs(app.model_gateway.lifecycle, app.components.lifecycle)
        self.assertIs(app.model_gateway.provider_selector, app.provider_selector)
        self.assertIs(app.model_gateway.provider_execution, app.invoker.provider_execution)
        self.assertIsInstance(app.capability_catalog, RegistryCapabilityCatalog)
        self.assertIsInstance(app.plan_validator, PlanValidator)
        self.assertIs(app.plan_validator.catalog, app.capability_catalog)
        self.assertIs(app.runtime.invoker, app.invoker)
        self.assertIs(app.runtime.lifecycle, app.components.lifecycle)
        self.assertIs(app.execution_engine.validator, app.plan_validator)
        self.assertIs(app.execution_engine.scheduler, app.components.scheduler)
        self.assertIsInstance(app.state_store, InMemoryStateStore)
        self.assertIs(app.execution_engine.state_store, app.state_store)
        self.assertEqual(len(app.policy_engine.policies), 1)
        self.assertIsInstance(app.policy_engine.policies[0], AllowAllPolicy)
        self.assertEqual(app.registry.list(), ())
        self.assertEqual(app.loaded_plugins, ())

    def test_custom_components_are_wired_by_identity(self) -> None:
        registry = InMemoryCapabilityRegistry()
        policy_engine = PolicyEngine((AllowAllPolicy(),))
        tracer = InMemoryTracer()
        context_factory = DefaultInvocationContextFactory()
        capability_catalog = RegistryCapabilityCatalog(registry)
        plan_validator = PlanValidator(capability_catalog)
        state_store = InMemoryStateStore()

        app = build_harness(
            registry=registry,
            policy_engine=policy_engine,
            tracer=tracer,
            context_factory=context_factory,
            capability_catalog=capability_catalog,
            plan_validator=plan_validator,
            state_store=state_store,
            entry_point_group=None,
        )

        self.assertIs(app.registry, registry)
        self.assertIs(app.policy_engine, policy_engine)
        self.assertIs(app.tracer, tracer)
        self.assertIs(app.components.context_factory, context_factory)
        self.assertIs(app.capability_catalog, capability_catalog)
        self.assertIs(app.plan_validator, plan_validator)
        self.assertIs(app.state_store, state_store)

    def test_rejects_ambiguous_policy_configuration(self) -> None:
        with self.assertRaises(ValueError):
            build_harness(
                policies=(AllowAllPolicy(),),
                policy_engine=PolicyEngine(),
                entry_point_group=None,
            )

    def test_rejects_mismatched_catalog_and_validator(self) -> None:
        first = RegistryCapabilityCatalog(InMemoryCapabilityRegistry())
        second = RegistryCapabilityCatalog(InMemoryCapabilityRegistry())

        with self.assertRaises(ValueError):
            build_harness(
                capability_catalog=first,
                plan_validator=PlanValidator(second),
                entry_point_group=None,
            )

    def test_rejects_plugins_with_custom_plugin_provider(self) -> None:
        plugin = StubPlugin("echo", (EchoTool(),))

        with self.assertRaises(ValueError):
            build_harness(
                plugins=(plugin,),
                plugin_provider=LocalPluginProvider(entry_point_group=None),
                entry_point_group=None,
            )


class BootstrapLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_plan_uses_bootstrapped_engine_and_invoker(self) -> None:
        tool = EchoTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            entry_point_group=None,
        )
        await app.start()
        request = Request(input=RequestInput(type="json", content={}))
        plan = ExecutionPlan(
            plan_id="bootstrap-plan",
            nodes=(
                PlanNode(
                    node_id="echo",
                    capability="echo.tool/v1",
                    input_mapping={"message": LiteralBinding(value="from plan")},
                ),
            ),
            outputs={
                "message": NodeOutputBinding(
                    node_id="echo",
                    pointer="/output/data/message",
                )
            },
        )

        result = await app.execute_plan(request, plan)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "from plan")
        self.assertIsNotNone(app.execution_engine.state("bootstrap-plan"))
        await app.shutdown()

    async def test_start_loads_plugin_and_runtime_can_invoke_it(self) -> None:
        tool = EchoTool()
        plugin = StubPlugin("echo-plugin", (tool,))
        app = build_harness(plugins=(plugin,), entry_point_group=None)

        loaded = await app.start()
        result = await app.invoke(make_request())

        self.assertEqual(app.state, BootstrapState.STARTED)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(plugin.initialize_count, 1)
        self.assertIsNotNone(app.registry.get("echo.tool/v1"))
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(tool.calls, 1)
        self.assertIsInstance(app.event_publisher, InMemoryEventBus)
        provider_events = [
            event.name
            for event in app.event_publisher.events()
            if event.name.value.startswith("provider.")
        ]
        self.assertEqual(
            provider_events,
            [
                ExecutionEventName.PROVIDER_CANDIDATES,
                ExecutionEventName.PROVIDER_SELECTED,
            ],
        )

        await app.shutdown()

        self.assertEqual(app.state, BootstrapState.STOPPED)
        self.assertEqual(plugin.shutdown_count, 1)
        self.assertIsNone(app.registry.get("echo.tool/v1"))

    async def test_start_and_shutdown_are_idempotent(self) -> None:
        plugin = StubPlugin("echo-plugin", (EchoTool(),))
        app = build_harness(plugins=(plugin,), entry_point_group=None)

        first = await app.start()
        second = await app.start()
        await app.shutdown()
        await app.shutdown()

        self.assertEqual(first, second)
        self.assertEqual(plugin.initialize_count, 1)
        self.assertEqual(plugin.shutdown_count, 1)

    async def test_invoke_requires_started_application(self) -> None:
        app = build_harness(entry_point_group=None)

        with self.assertRaises(BootstrapStateError):
            await app.invoke(make_request())

        await app.start()
        await app.shutdown()

        with self.assertRaises(BootstrapStateError):
            await app.invoke(make_request())

    async def test_cancel_plan_requires_started_application_and_delegates(self) -> None:
        app = build_harness(entry_point_group=None)

        with self.assertRaises(BootstrapStateError):
            await app.cancel_plan("missing")

        await app.start()
        self.assertIs(await app.cancel_plan("missing"), False)
        await app.shutdown()

    async def test_stopped_application_cannot_restart(self) -> None:
        app = build_harness(entry_point_group=None)
        await app.start()
        await app.shutdown()

        with self.assertRaises(BootstrapStateError):
            await app.start()

    async def test_async_context_manager_starts_and_shuts_down(self) -> None:
        plugin = StubPlugin("echo-plugin", (EchoTool(),))
        app = build_harness(plugins=(plugin,), entry_point_group=None)

        async with app as running:
            self.assertIs(running, app)
            self.assertEqual(app.state, BootstrapState.STARTED)
            self.assertIsNotNone(app.registry.get("echo.tool/v1"))

        self.assertEqual(app.state, BootstrapState.STOPPED)
        self.assertEqual(plugin.shutdown_count, 1)
        self.assertIsNone(app.registry.get("echo.tool/v1"))

    async def test_startup_failure_rolls_back_batch_and_keeps_created_state(self) -> None:
        first = StubPlugin("first", (EchoTool("shared.tool/v1"),))
        second = StubPlugin("second", (EchoTool("shared.tool/v1", version="2.0.0"),))
        app = build_harness(
            plugins=(first, second),
            entry_point_group=None,
        )

        with self.assertRaises(PluginError):
            await app.start()

        self.assertEqual(app.state, BootstrapState.CREATED)
        self.assertEqual(app.registry.list(), ())
        self.assertEqual(app.loaded_plugins, ())
        self.assertEqual(first.shutdown_count, 1)
        self.assertEqual(second.shutdown_count, 1)

    async def test_injected_policy_controls_runtime_without_plugin_awareness(self) -> None:
        tool = EchoTool()
        plugin = StubPlugin("echo-plugin", (tool,))
        app = build_harness(
            plugins=(plugin,),
            policies=(DenyAllPolicy(),),
            entry_point_group=None,
        )
        await app.start()

        result = await app.invoke(make_request())

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(tool.calls, 0)
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
