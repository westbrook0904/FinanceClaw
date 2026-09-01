"""Portfolio Risk Agent 的本地插件包装。"""

from __future__ import annotations

from harness_spi import Capability, PluginManifest, PluginSPI

from .agent import PORTFOLIO_RISK_CAPABILITY_ID, PortfolioRiskAgent


class PortfolioRiskAgentPlugin(PluginSPI):
    def __init__(self) -> None:
        self._agent = PortfolioRiskAgent()
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="portfolio-risk-agent",
            name="Portfolio Risk Agent",
            version="1.0.0",
            sdk_version="1",
            capabilities=(PORTFOLIO_RISK_CAPABILITY_ID,),
            metadata={"example": True, "real_use": True, "business_plugin": True},
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return (self._agent,)

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False
