import pytest
from langchain_core.tools import BaseTool

from financeclaw_spike.mcp import MCPToolUnavailable, load_demo_mcp_tool


@pytest.mark.asyncio
async def test_stateless_mcp_tool_converts_to_base_tool_and_returns_content_blocks() -> None:
    tool, governance = await load_demo_mcp_tool()

    result = await tool.ainvoke({"symbol": "AAPL"})

    assert isinstance(tool, BaseTool)
    assert tool.name == "get_demo_quote"
    assert governance.side_effect == "READ"
    assert governance.approval == "NONE"
    assert isinstance(result, list)
    assert result[0]["type"] == "text"
    assert "stage0-mcp-demo" in result[0]["text"]


@pytest.mark.asyncio
async def test_mcp_loader_has_bounded_timeout() -> None:
    with pytest.raises(MCPToolUnavailable):
        await load_demo_mcp_tool(timeout_seconds=1e-9)
