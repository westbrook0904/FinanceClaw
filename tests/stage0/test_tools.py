import json

import pytest
from pydantic import ValidationError

from financeclaw_spike.tools import DemoReadTool, DemoWriteTool, TransientReadError


def test_read_tool_schema_and_retryable_failure() -> None:
    tool = DemoReadTool(fail_first=1)

    with pytest.raises(TransientReadError):
        tool.invoke({"symbol": "AAPL"})

    result = json.loads(tool.invoke({"symbol": "AAPL"}))
    assert result == {
        "as_of": "2026-09-02T00:00:00Z",
        "currency": "USD",
        "price": "100.00",
        "provider": "stage0-demo",
        "symbol": "AAPL",
    }
    assert tool.call_count == 2


def test_read_tool_rejects_invalid_symbol() -> None:
    with pytest.raises(ValidationError):
        DemoReadTool().invoke({"symbol": "AAPL; DROP TABLE"})


@pytest.mark.asyncio
async def test_write_tool_async_contract() -> None:
    tool = DemoWriteTool()

    result = json.loads(await tool.ainvoke({"symbol": "msft", "note": "demo"}))

    assert result == {"note": "demo", "status": "written", "symbol": "MSFT"}
    assert tool.writes == ({"symbol": "MSFT", "note": "demo"},)
