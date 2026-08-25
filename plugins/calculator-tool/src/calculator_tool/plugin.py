"""Calculator Tool 的阶段一本地插件包装。"""

from __future__ import annotations

from harness_spi import Capability, PluginManifest, PluginSPI

from .tool import CalculatorTool


class CalculatorToolPlugin(PluginSPI):
    """向 LocalPluginLoader 暴露稳定的 ``CalculatorTool`` Provider。"""

    def __init__(self) -> None:
        self._tool = CalculatorTool()
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="calculator-tool",
            name="Calculator Tool",
            version="1.0.0",
            sdk_version="1",
            capabilities=(self._tool.descriptor().id,),
            metadata={"example": True},
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return (self._tool,)

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False
