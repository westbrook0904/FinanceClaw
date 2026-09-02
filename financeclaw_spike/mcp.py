"""Stateless MCP loading with a local governance overlay and bounded timeout."""

import asyncio
import sys
from dataclasses import dataclass

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient


class MCPToolUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MCPGovernanceOverlay:
    tool_name: str
    side_effect: str
    approval: str
    egress: str
    retryable: bool


DEMO_MCP_GOVERNANCE = MCPGovernanceOverlay(
    tool_name="get_demo_quote",
    side_effect="READ",
    approval="NONE",
    egress="LOCAL_PROCESS",
    retryable=True,
)


async def load_demo_mcp_tool(
    *, timeout_seconds: float = 10.0
) -> tuple[BaseTool, MCPGovernanceOverlay]:
    """Load one tool; MultiServerMCPClient creates a fresh session per invocation."""

    client = MultiServerMCPClient(
        {
            "financeclaw_demo": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "financeclaw_spike.mcp_server"],
            }
        }
    )
    try:
        tools = await asyncio.wait_for(client.get_tools(), timeout=timeout_seconds)
    except (TimeoutError, OSError, RuntimeError) as exc:
        raise MCPToolUnavailable("demo MCP server is unavailable") from exc
    matching = [tool for tool in tools if tool.name == DEMO_MCP_GOVERNANCE.tool_name]
    if len(matching) != 1:
        raise MCPToolUnavailable("demo MCP tool schema was not returned exactly once")
    return matching[0], DEMO_MCP_GOVERNANCE
