"""暴露 FinanceClaw 示例行情 MCP 服务端工具。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("financeclaw-stage1-demo")


@server.tool()
def get_demo_quote(symbol: str) -> dict[str, str | bool]:
    """按标识读取mcp server 模块的数据；不存在时由下层仓储抛出明确异常。"""
    return {
        "symbol": symbol.upper(),
        "price": "100.00",
        "currency": "USD",
        "provider": "financeclaw-stage1-mcp",
        "as_of": "2026-09-02T00:00:00Z",
        "fallback_used": False,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
