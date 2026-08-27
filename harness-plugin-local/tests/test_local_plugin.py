"""本地插件发现、生命周期、Provider 注册和回滚测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    PluginError,
    ResultEnvelope,
)
from harness_plugin_local import LocalPluginLoader, LocalPluginProvider, PluginState
from harness_registry import CapabilityQuery, InMemoryCapabilityRegistry, legacy_provider_id
from harness_spi import AgentRequest, AgentSPI, PluginManifest, PluginSPI, ToolRequest, ToolSPI


class StubAgent(AgentSPI):
    def __init__(self, capability_id: str, *, version: str = "1.0.0") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.AGENT,
            version=version,
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise NotImplementedError


class StubTool(ToolSPI):
    def __init__(self, capability_id: str, *, version: str = "1.0.0") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version=version,
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise NotImplementedError


class StubPlugin(PluginSPI):
    def __init__(self, plugin_id: str, providers: tuple[AgentSPI | ToolSPI, ...]) -> None:
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

    def capabilities(self) -> tuple[AgentSPI | ToolSPI, ...]:
        return self.providers

    async def initialize(self) -> None:
        self.initialize_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


class LocalPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = InMemoryCapabilityRegistry()

    async def test_load_registers_provider_ids_and_unload_cleans_them(self) -> None:
        plugin = StubPlugin(
            "mixed-plugin",
            (StubAgent("echo.reply/v1"), StubTool("math.add/v1")),
        )
        loader = LocalPluginLoader(
            self.registry,
            LocalPluginProvider((plugin,), entry_point_group=None),
        )

        loaded = await loader.load_all()

        self.assertEqual(loaded[0].state, PluginState.ACTIVE)
        self.assertEqual(plugin.initialize_count, 1)
        self.assertEqual(
            loaded[0].provider_ids,
            (
                legacy_provider_id("mixed-plugin", "echo.reply/v1"),
                legacy_provider_id("mixed-plugin", "math.add/v1"),
            ),
        )
        self.assertEqual(len(self.registry.list_providers()), 2)
        self.assertIs(
            self.registry.resolve(CapabilityQuery(id="echo.reply/v1")).provider,
            plugin.providers[0],
        )

        stopped = await loader.unload("mixed-plugin")
        self.assertEqual(stopped.state, PluginState.STOPPED)
        self.assertEqual(plugin.shutdown_count, 1)
        self.assertEqual(self.registry.list_providers(), ())

    async def test_two_plugins_can_provide_same_capability_and_unload_independently(self) -> None:
        first = StubPlugin("first", (StubTool("shared/v1"),))
        second = StubPlugin("second", (StubTool("shared/v1"),))
        loader = LocalPluginLoader(
            self.registry,
            LocalPluginProvider((first, second), entry_point_group=None),
        )

        loaded = await loader.load_all()

        self.assertEqual(len(loaded), 2)
        self.assertEqual(len(self.registry.candidates("shared/v1")), 2)
        await loader.unload("first")
        remaining = self.registry.candidates("shared/v1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].plugin_id, "second")

    async def test_registration_failure_rolls_back_registered_provider_ids(self) -> None:
        existing = StubTool("conflict/v1", version="1.0.0")
        self.registry.register(existing, plugin_id="existing")
        plugin = StubPlugin(
            "failing-plugin",
            (
                StubTool("temporary/v1"),
                StubTool("conflict/v1", version="2.0.0"),
            ),
        )
        loader = LocalPluginLoader(self.registry)

        with self.assertRaises(PluginError):
            await loader.load(plugin)

        self.assertEqual(plugin.initialize_count, 1)
        self.assertEqual(plugin.shutdown_count, 1)
        self.assertEqual(self.registry.candidates("temporary/v1"), ())
        self.assertIs(self.registry.get("conflict/v1").provider, existing)
        self.assertEqual(loader.loaded_plugins(), ())

    async def test_manifest_mismatch_is_rejected_before_initialization(self) -> None:
        plugin = StubPlugin("invalid-plugin", (StubTool("actual/v1"),))
        original_manifest = plugin.manifest
        plugin.manifest = lambda: original_manifest().model_copy(  # type: ignore[method-assign]
            update={"capabilities": ("declared/v1",)}
        )
        loader = LocalPluginLoader(self.registry)

        with self.assertRaises(PluginError):
            await loader.load(plugin)

        self.assertEqual(plugin.initialize_count, 0)
        self.assertEqual(self.registry.list_providers(), ())

    async def test_load_all_rolls_back_batch_on_capability_contract_mismatch(self) -> None:
        first = StubPlugin("first", (StubTool("shared/v1", version="1.0.0"),))
        second = StubPlugin("second", (StubTool("shared/v1", version="2.0.0"),))
        loader = LocalPluginLoader(
            self.registry,
            LocalPluginProvider((first, second), entry_point_group=None),
        )

        with self.assertRaises(PluginError):
            await loader.load_all()

        self.assertEqual(self.registry.list_providers(), ())
        self.assertEqual(first.shutdown_count, 1)
        self.assertEqual(second.shutdown_count, 1)
        self.assertEqual(loader.loaded_plugins(), ())

    def test_provider_rejects_duplicate_discovered_plugin_ids(self) -> None:
        first = StubPlugin("duplicate", (StubTool("first/v1"),))
        second = StubPlugin("duplicate", (StubTool("second/v1"),))
        provider = LocalPluginProvider((first, second), entry_point_group=None)

        with self.assertRaises(PluginError):
            provider.discover()


if __name__ == "__main__":
    unittest.main()
