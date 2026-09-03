"""把远端 MCP 行情能力适配为受治理的 LangChain 工具。"""

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
    """定义MCP工具Unavailable。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class MCPQuoteInput(BaseModel):
    """定义MCPQuote的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        symbol: 标准化金融标的代码。
    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class MCPQuoteTool(BaseTool):
    """定义MCPQuote工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
        _timeout_seconds: 该操作允许的最长时间（秒）。
    """

    name: str = "get_demo_quote"
    description: str = "Read a demo quote from the configured stateless MCP service."
    args_schema: type[BaseModel] = MCPQuoteInput

    _timeout_seconds: float = PrivateAttr(default=10.0)

    def __init__(self, *, timeout_seconds: float = 10.0, **kwargs: Any) -> None:
        """注入并保存MCPQuote工具所需的协作对象，同时校验构造期不变量。"""
        super().__init__(**kwargs)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    async def _delegate(self, symbol: str) -> Any:
        """连接 MCP 服务、加载指定工具并在超时限制内转发行情请求。"""
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
            tools = await asyncio.wait_for(client.get_tools(), timeout=self._timeout_seconds)
            matching = [tool for tool in tools if tool.name == self.name]
            if len(matching) != 1:
                raise MCPToolUnavailable("MCP tool schema was not returned exactly once")
            return await asyncio.wait_for(
                matching[0].ainvoke({"symbol": symbol}), timeout=self._timeout_seconds
            )
        except (TimeoutError, OSError, RuntimeError) as exc:
            if isinstance(exc, MCPToolUnavailable):
                raise
            raise MCPToolUnavailable("MCP tool is unavailable") from exc

    def _run(self, symbol: str) -> Any:
        """执行工具的同步实现，并返回可序列化结果。"""
        return asyncio.run(self._delegate(symbol))

    async def _arun(self, symbol: str) -> Any:
        """执行工具的异步实现，保持与同步入口相同的业务语义。"""
        return await self._delegate(symbol)


def managed_mcp_quote_tool(*, timeout_seconds: float = 10.0) -> ManagedTool:
    """构造带只读、外部出站和瞬时重试治理元数据的 MCP 行情工具。"""
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
