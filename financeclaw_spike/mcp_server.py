"""Tiny stdio MCP server used to prove MCP-to-BaseTool conversion."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("financeclaw-stage0-demo")


@mcp.tool()
def get_demo_quote(symbol: str) -> dict[str, str]:
    """Return a deterministic quote with source and freshness metadata."""

    return {
        "symbol": symbol.upper(),
        "price": "100.00",
        "currency": "USD",
        "provider": "stage0-mcp-demo",
        "as_of": "2026-09-02T00:00:00Z",
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
