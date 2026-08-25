"""Echo Agent 的阶段一本地插件包装。"""

from __future__ import annotations

from harness_spi import Capability, PluginManifest, PluginSPI

from .agent import EchoAgent


class EchoAgentPlugin(PluginSPI):
    """向 LocalPluginLoader 暴露一个稳定的 ``EchoAgent`` Provider。"""

    def __init__(self) -> None:
        self._agent = EchoAgent()
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="echo-agent",
            name="Echo Agent",
            version="1.0.0",
            sdk_version="1",
            capabilities=(self._agent.descriptor().id,),
            metadata={"example": True},
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return (self._agent,)

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False
