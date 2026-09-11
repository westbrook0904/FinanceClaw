"""Use real product graphs with fresh synthetic quotes for persistent Workflow approval tests."""

import json
from datetime import UTC, datetime
from functools import lru_cache

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.memory.history import HistoryService
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.history import history_tools
from financeclaw.agent_server.tools.local import MarketSnapshotTool, default_local_tools
from financeclaw.agent_server.tools.mcp import managed_mcp_quote_tool
from financeclaw.agent_server.tools.memory import default_memory_tools
from financeclaw.shared.infrastructure.runtime import process_resources


class FreshSyntheticQuote(MarketSnapshotTool):
    """Keep demo prices and governance; inject current synthetic data generation time."""

    def _run(self, symbol):
        """Return synthetic data with an honest test-generation timestamp."""
        result = json.loads(super()._run(symbol))
        result["as_of"] = datetime.now(UTC).isoformat()
        return json.dumps(result)


@lru_cache(maxsize=1)
def graph():
    """Assemble unmodified product orchestration with one explicitly injected test data source."""
    resources = process_resources()
    assert resources.settings.environment.value == "test" and resources.settings.offline_model
    defaults = build_components(resources=resources, enable_persistence=True)
    catalog = ToolCatalog(
        (
            *default_local_tools(market_tool=FreshSyntheticQuote()),
            managed_mcp_quote_tool(timeout_seconds=resources.settings.mcp_timeout_seconds),
            *default_memory_tools(defaults.memory_service),
            *history_tools(
                HistoryService(resources.conversation_repository, resources.artifact_service)
            ),
        )
    )
    components = build_components(
        resources=resources, tool_catalog=catalog, enable_persistence=True
    )
    return components.agent_factory.build(
        components.default_agent_profile, model=OfflineFinanceModel(), checkpointer=None
    )


async def finance_agent(config):
    """Let the real native worker inject its durable checkpointer and Store."""
    return graph()
