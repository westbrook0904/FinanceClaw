"""Bootstrap 的可选 Memory 组合与无 Memory 降级测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_context import MemoryContextSource
from harness_memory import InMemoryMemoryProvider, MemoryGateway, MemoryPolicy
from harness_policy import AllowAllPolicy, PolicyEngine


class MemoryCompositionTests(unittest.TestCase):
    def test_default_build_has_no_memory_dependency(self) -> None:
        app = build_harness(entry_point_group=None)

        self.assertIsNone(app.memory_provider)
        self.assertIsNone(app.memory_gateway)
        self.assertFalse(
            any(isinstance(source, MemoryContextSource) for source in app.context_pipeline.sources)
        )

    def test_provider_builds_gateway_and_memory_context_source(self) -> None:
        provider = InMemoryMemoryProvider()
        app = build_harness(
            memory_provider=provider,
            memory_namespaces={"profile"},
            entry_point_group=None,
        )

        self.assertIs(app.memory_provider, provider)
        self.assertIsNotNone(app.memory_gateway)
        memory_sources = tuple(
            source
            for source in app.context_pipeline.sources
            if isinstance(source, MemoryContextSource)
        )
        self.assertEqual(len(memory_sources), 1)
        self.assertIs(memory_sources[0].gateway, app.memory_gateway)
        self.assertIs(
            app.memory_gateway.policy.policy_engine,
            app.policy_engine,
        )

    def test_memory_configuration_rejects_ambiguous_or_mismatched_governance(self) -> None:
        policy_engine = PolicyEngine((AllowAllPolicy(),))
        gateway = MemoryGateway(
            InMemoryMemoryProvider(),
            MemoryPolicy(policy_engine),
            allowed_namespaces={"profile"},
        )
        with self.assertRaises(ValueError):
            build_harness(
                memory_provider=InMemoryMemoryProvider(),
                memory_gateway=gateway,
                memory_namespaces={"profile"},
                entry_point_group=None,
            )
        with self.assertRaises(ValueError):
            build_harness(
                policy_engine=PolicyEngine((AllowAllPolicy(),)),
                memory_gateway=gateway,
                entry_point_group=None,
            )


if __name__ == "__main__":
    unittest.main()
