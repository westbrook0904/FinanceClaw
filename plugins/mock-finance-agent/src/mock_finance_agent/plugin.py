"""Mock Finance Agent 的阶段一本地插件包装。"""

from __future__ import annotations

from harness_spi import Capability, PluginManifest, PluginSPI

from .agent import MockFinanceAgent


class MockFinanceAgentPlugin(PluginSPI):
    """边界验证插件；Harness Core 不需要知道任何财经类型。"""

    def __init__(self) -> None:
        self._agent = MockFinanceAgent()
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="mock-finance-agent",
            name="Mock Finance Agent",
            version="1.0.0",
            sdk_version="1",
            capabilities=(self._agent.descriptor().id,),
            metadata={"example": True, "mock": True},
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return (self._agent,)

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False
