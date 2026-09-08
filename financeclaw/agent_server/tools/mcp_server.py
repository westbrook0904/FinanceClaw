"""把受治理的演示行情报价 Tool 暴露为 MCP Server 的进程入口。

属于 orchestration/tools 治理层的对外暴露端：以 stdio 传输运行，
供 mcp.py 的适配器按需拉起，向 MCP 生态复用平台内的治理 Tool。
"""

from mcp.server.fastmcp import FastMCP

# MCP Server 实例：以 stdio 传输运行，服务名用于客户端识别与审计归因。
server = FastMCP("financeclaw-stage1-demo")


@server.tool()
def get_demo_quote(symbol: str) -> dict[str, str | bool]:
    """返回指定证券的演示行情报价，附带提供方与 as-of 证据字段。

    供通过 MCP 协议接入的外部客户端（如 mcp.py 的适配器）调用；
    入参为证券代码，返回包含价格、币种、提供方与时间戳的字典。

    Args:
        symbol: 证券代码，返回前统一转为大写。

    Returns:
        演示报价字典，含 symbol、price、currency、provider、as_of
        与 fallback_used 字段。

    """
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
