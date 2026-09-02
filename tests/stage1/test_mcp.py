import pytest

from financeclaw.tools import Egress, SideEffect, managed_mcp_quote_tool


@pytest.mark.asyncio
async def test_mcp_tool_is_basetool_with_local_governance_overlay() -> None:
    managed = managed_mcp_quote_tool(timeout_seconds=10)
    result = await managed.tool.ainvoke({"symbol": "AAPL"})

    assert managed.tool.name == "get_demo_quote"
    assert managed.governance.side_effect is SideEffect.READ
    assert managed.governance.egress is Egress.INTERNAL
    assert managed.governance.required_scopes == frozenset({"market:read"})
    assert "financeclaw-stage1-mcp" in str(result)
