"""通过 MCP 协议接入外部 Tool 的适配实现：演示行情报价 Tool。

属于 orchestration/tools 治理层的实现模块，基于 langchain-mcp-adapters
把远端 MCP Server 上的工具包装为受治理的 ManagedTool，超时或服务不可
用时统一转为可重试的瞬态错误。
"""

import asyncio
import sys
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field, PrivateAttr

from financeclaw.kernel import DataClassification

from .governance import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)


class MCPToolUnavailable(RuntimeError):
    """MCP 服务或目标工具不可用时抛出的运行时异常。

    使用场景：MCP 连接、工具发现或调用超时/失败时抛出，向调用方
    屏蔽底层协议细节；治理层可据此触发回退或向用户解释服务暂不可用。
    """

    pass


class MCPQuoteInput(BaseModel):
    """get_demo_quote Tool 的入参模型：单个证券代码。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema。

    Attributes:
        symbol: 证券代码，1~16 位，仅允许字母、数字与 ``. _ -``。

    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class MCPQuoteTool(BaseTool):
    """从无状态 MCP 服务读取演示行情报价的 Tool。

    使用场景：供具备 market:read 作用域的行情类 Agent 调用；每次调用
    通过 langchain-mcp-adapters 以 stdio 方式拉起 MCP Server、发现同名
    工具并转发调用，返回带提供方与 as-of 证据的报价结果。连接、发现
    与调用均在超时预算内执行，失败统一转为 MCPToolUnavailable。

    Attributes:
        name: Tool 名称，固定为 ``get_demo_quote``，需与 MCP 端工具同名。
        description: 展示给 Agent 的工具用途说明。
        args_schema: 入参模型，见 MCPQuoteInput。
        _timeout_seconds: 连接、工具发现与单次调用的超时预算（秒）。

    """

    name: str = "get_demo_quote"
    description: str = "Read a demo quote from the configured stateless MCP service."
    args_schema: type[BaseModel] = MCPQuoteInput

    _timeout_seconds: float = PrivateAttr(default=10.0)

    def __init__(self, *, timeout_seconds: float = 10.0, **kwargs: Any) -> None:
        """初始化 MCP 报价 Tool。

        Args:
            timeout_seconds: 连接、工具发现与调用的超时预算（秒），
                必须为正数。
            **kwargs: 透传给 ``BaseTool`` 的其余字段。

        Raises:
            ValueError: timeout_seconds 不为正数。

        """
        super().__init__(**kwargs)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    async def _delegate(self, symbol: str) -> Any:
        """把一次报价请求委托给 MCP Server 上的同名工具执行。

        Args:
            symbol: 证券代码，原样透传给 MCP 工具。

        Returns:
            MCP 工具返回的原始结果。

        Raises:
            MCPToolUnavailable: 连接、工具发现或调用超时失败，或 MCP 端
                未恰好返回一个同名工具。

        """
        # 1. 以 stdio 方式配置无状态 MCP Server（本模块的 mcp_server）。
        client = MultiServerMCPClient(
            {
                "financeclaw_demo": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "financeclaw.orchestration.tools.mcp_server"],
                }
            }
        )
        try:
            # 2. 在超时预算内发现工具，并要求同名工具恰好只有一个。
            tools = await asyncio.wait_for(client.get_tools(), timeout=self._timeout_seconds)
            matching = [tool for tool in tools if tool.name == self.name]
            if len(matching) != 1:
                raise MCPToolUnavailable("MCP tool schema was not returned exactly once")
            # 3. 转发调用并在超时预算内等待结果。
            return await asyncio.wait_for(
                matching[0].ainvoke({"symbol": symbol}), timeout=self._timeout_seconds
            )
        except (TimeoutError, OSError, RuntimeError) as exc:
            # 4. 统一把协议层失败转为瞬态不可用错误，保留原有异常链。
            if isinstance(exc, MCPToolUnavailable):
                raise
            raise MCPToolUnavailable("MCP tool is unavailable") from exc

    def _run(self, symbol: str) -> Any:
        """同步入口：在独立事件循环中执行异步委托。"""
        return asyncio.run(self._delegate(symbol))

    async def _arun(self, symbol: str) -> Any:
        """异步入口：直接执行异步委托，供 LangChain 异步调用链使用。"""
        return await self._delegate(symbol)


def managed_mcp_quote_tool(*, timeout_seconds: float = 10.0) -> ManagedTool:
    """装配带治理元数据的 MCP 报价 Tool。

    Args:
        timeout_seconds: 透传给 ``MCPQuoteTool`` 的超时预算（秒）。

    Returns:
        包装完成的 ManagedTool：只读、瞬态可重试、需 market:read
        作用域，数据密级限制为公开、内部与机密。

    """
    return ManagedTool(
        MCPQuoteTool(timeout_seconds=timeout_seconds),
        ToolGovernance(
            tool_id="get_demo_quote",
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"market:read"}),
            approval=ApprovalMode.NONE,
            egress=Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.TRANSIENT_READ,
            audit_level=AuditLevel.FULL,
            allowed_data_classes=frozenset(
                {
                    DataClassification.PUBLIC,
                    DataClassification.INTERNAL,
                    DataClassification.CONFIDENTIAL,
                }
            ),
        ),
    )
