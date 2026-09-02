"""Stateless demo MCP server used by the Stage-1 integration boundary."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("financeclaw-stage1-demo")


@server.tool()
def get_demo_quote(symbol: str) -> dict[str, str | bool]:
    """Return a bounded quote with source and freshness metadata."""

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
