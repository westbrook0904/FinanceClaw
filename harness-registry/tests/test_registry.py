"""内存 Capability / Provider Registry 的行为测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    InvocationContext,
    ProviderDescriptor,
    ProviderError,
    RegistryError,
    ResultEnvelope,
    SideEffectType,
)
from harness_registry import (
    CapabilityQuery,
    InMemoryCapabilityRegistry,
    ProviderQuery,
    RegistryCapabilityCatalog,
    legacy_provider_id,
)
from harness_spi import ToolRequest, ToolSPI


class StubTool(ToolSPI):
    def __init__(
        self,
        capability_id: str,
        *,
        version: str = "1.0.0",
        tags: frozenset[str] = frozenset(),
        side_effect: SideEffectType = SideEffectType.NONE,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version=version,
            tags=tags,
            execution_profile=CapabilityExecutionProfile(side_effect=side_effect),
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise NotImplementedError


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = InMemoryCapabilityRegistry()
        self.add = StubTool("math.add/v1", tags=frozenset({"math", "stable"}))
        self.subtract = StubTool("math.subtract/v1", tags=frozenset({"math"}))

    def test_legacy_register_get_list_and_unregister_still_work(self) -> None:
        registered = self.registry.register(self.add, plugin_id="calculator")

        self.assertIs(self.registry.get("math.add/v1"), registered)
        self.assertEqual(self.registry.list(), (registered,))
        self.assertEqual(
            registered.provider_id,
            legacy_provider_id("calculator", "math.add/v1"),
        )
        removed = self.registry.unregister("math.add/v1", plugin_id="calculator")
        self.assertIs(removed.provider, self.add)
        self.assertIsNone(self.registry.get("math.add/v1"))

    def test_same_capability_can_have_multiple_providers(self) -> None:
        provider_a = StubTool("math.add/v1")
        provider_b = StubTool("math.add/v1")

        self.registry.register(provider_a, plugin_id="calculator-a")
        self.registry.register(provider_b, plugin_id="calculator-b")

        candidates = self.registry.candidates("math.add/v1")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            tuple(item.provider_id for item in candidates),
            tuple(
                sorted(
                    (
                        legacy_provider_id("calculator-a", "math.add/v1"),
                        legacy_provider_id("calculator-b", "math.add/v1"),
                    )
                )
            ),
        )
        with self.assertRaises(RegistryError) as raised:
            self.registry.resolve(CapabilityQuery(id="math.add/v1"))
        self.assertEqual(raised.exception.code, "HARNESS.REGISTRY.AMBIGUOUS")

    def test_unregister_provider_does_not_remove_other_candidate(self) -> None:
        self.registry.register(StubTool("math.add/v1"), plugin_id="calculator-a")
        self.registry.register(StubTool("math.add/v1"), plugin_id="calculator-b")
        provider_a = legacy_provider_id("calculator-a", "math.add/v1")

        removed = self.registry.unregister_provider(provider_a)

        self.assertEqual(removed.provider_id, provider_a)
        remaining = self.registry.candidates("math.add/v1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].plugin_id, "calculator-b")
        self.assertIsNotNone(self.registry.get_capability_descriptor("math.add/v1"))

    def test_register_provider_rejects_duplicate_provider_id(self) -> None:
        provider_id = "provider-a"
        self.registry.register_provider(
            StubTool("math.add/v1"),
            descriptor=ProviderDescriptor(
                provider_id=provider_id,
                capability_id="math.add/v1",
                plugin_id="calculator-a",
                implementation_version="1.0.0",
            ),
        )

        with self.assertRaises(ProviderError) as raised:
            self.registry.register_provider(
                StubTool("math.add/v1"),
                descriptor=ProviderDescriptor(
                    provider_id=provider_id,
                    capability_id="math.add/v1",
                    plugin_id="calculator-b",
                    implementation_version="1.0.0",
                ),
            )

        self.assertEqual(raised.exception.code, "HARNESS.PROVIDER.DUPLICATE")

    def test_register_provider_rejects_capability_contract_mismatch(self) -> None:
        self.registry.register(StubTool("mail.send/v1"), plugin_id="mail-a")

        with self.assertRaises(ProviderError) as raised:
            self.registry.register_provider(
                StubTool("mail.send/v1", side_effect=SideEffectType.WRITE),
                descriptor=ProviderDescriptor(
                    provider_id="mail-b",
                    capability_id="mail.send/v1",
                    plugin_id="mail-b",
                    implementation_version="1.0.0",
                ),
            )

        self.assertEqual(
            raised.exception.code,
            "HARNESS.PROVIDER.CAPABILITY_MISMATCH",
        )
        self.assertEqual(len(self.registry.candidates("mail.send/v1")), 1)

    def test_legacy_unregister_still_checks_plugin_ownership(self) -> None:
        self.registry.register(self.add, plugin_id="calculator")

        with self.assertRaises(RegistryError) as raised:
            self.registry.unregister("math.add/v1", plugin_id="another-plugin")

        self.assertEqual(raised.exception.code, "HARNESS.REGISTRY.OWNER_MISMATCH")
        self.assertIsNotNone(self.registry.get("math.add/v1"))

    def test_list_filters_with_and_semantics(self) -> None:
        self.registry.register(self.add, plugin_id="calculator")
        self.registry.register(self.subtract, plugin_id="calculator")

        matches = self.registry.list(
            CapabilityQuery(
                type=CapabilityType.TOOL,
                tags={"stable", "math"},
                plugin_id="calculator",
            )
        )

        self.assertEqual(tuple(item.descriptor.id for item in matches), ("math.add/v1",))

    def test_provider_query_filters_provider_metadata(self) -> None:
        self.registry.register_provider(
            StubTool("math.add/v1"),
            descriptor=ProviderDescriptor(
                provider_id="provider-sg",
                capability_id="math.add/v1",
                plugin_id="calculator",
                implementation_version="1.0.0",
                tags={"primary"},
                region="sg",
            ),
        )
        self.registry.register_provider(
            StubTool("math.add/v1"),
            descriptor=ProviderDescriptor(
                provider_id="provider-us",
                capability_id="math.add/v1",
                plugin_id="calculator-us",
                implementation_version="1.0.0",
                tags={"backup"},
                region="us",
            ),
        )

        matches = self.registry.list_providers(
            ProviderQuery(region="sg", provider_tags={"primary"})
        )

        self.assertEqual(tuple(item.provider_id for item in matches), ("provider-sg",))

    def test_catalog_deduplicates_provider_candidates(self) -> None:
        provider_a = StubTool("math.add/v1")
        provider_b = StubTool("math.add/v1")
        self.registry.register(provider_a, plugin_id="calculator-a")
        self.registry.register(provider_b, plugin_id="calculator-b")
        catalog = RegistryCapabilityCatalog(self.registry)

        descriptor = catalog.get("math.add/v1")

        self.assertIs(descriptor, provider_a.descriptor())
        self.assertEqual(catalog.list(), (provider_a.descriptor(),))
        self.assertFalse(hasattr(descriptor, "provider"))


if __name__ == "__main__":
    unittest.main()
