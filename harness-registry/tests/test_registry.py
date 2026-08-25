"""内存 Capability Registry 的行为测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    RegistryError,
    ResultEnvelope,
)
from harness_registry import (
    CapabilityQuery,
    InMemoryCapabilityRegistry,
    RegistryCapabilityCatalog,
)
from harness_spi import ToolRequest, ToolSPI


class StubTool(ToolSPI):
    def __init__(
        self,
        capability_id: str,
        *,
        version: str = "1.0.0",
        tags: frozenset[str] = frozenset(),
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version=version,
            tags=tags,
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

    def test_register_get_list_and_unregister(self) -> None:
        registered = self.registry.register(self.add, plugin_id="calculator")

        self.assertIs(self.registry.get("math.add/v1"), registered)
        self.assertEqual(self.registry.list(), (registered,))
        removed = self.registry.unregister("math.add/v1", plugin_id="calculator")
        self.assertIs(removed, registered)
        self.assertIsNone(self.registry.get("math.add/v1"))

    def test_register_rejects_duplicate_capability_id(self) -> None:
        self.registry.register(self.add, plugin_id="calculator-a")

        with self.assertRaises(RegistryError) as raised:
            self.registry.register(StubTool("math.add/v1"), plugin_id="calculator-b")

        self.assertEqual(raised.exception.code, "HARNESS.REGISTRY.DUPLICATE")

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

    def test_resolve_requires_exactly_one_match(self) -> None:
        self.registry.register(self.add, plugin_id="calculator")
        self.registry.register(self.subtract, plugin_id="calculator")

        resolved = self.registry.resolve(CapabilityQuery(id="math.add/v1"))
        self.assertIs(resolved.provider, self.add)

        with self.assertRaises(RegistryError) as missing:
            self.registry.resolve(CapabilityQuery(id="math.multiply/v1"))
        self.assertEqual(missing.exception.code, "HARNESS.REGISTRY.NOT_FOUND")

        with self.assertRaises(RegistryError) as ambiguous:
            self.registry.resolve(CapabilityQuery(tags={"math"}))
        self.assertEqual(ambiguous.exception.code, "HARNESS.REGISTRY.AMBIGUOUS")

    def test_unregister_checks_plugin_ownership(self) -> None:
        self.registry.register(self.add, plugin_id="calculator")

        with self.assertRaises(RegistryError) as raised:
            self.registry.unregister("math.add/v1", plugin_id="another-plugin")

        self.assertEqual(raised.exception.code, "HARNESS.REGISTRY.OWNER_MISMATCH")
        self.assertIsNotNone(self.registry.get("math.add/v1"))

    def test_catalog_exposes_descriptors_without_provider_instances(self) -> None:
        self.registry.register(self.add, plugin_id="calculator")
        catalog = RegistryCapabilityCatalog(self.registry)

        descriptor = catalog.get("math.add/v1")

        self.assertIs(descriptor, self.add.descriptor())
        self.assertEqual(catalog.list(), (self.add.descriptor(),))
        self.assertFalse(hasattr(descriptor, "provider"))


if __name__ == "__main__":
    unittest.main()
