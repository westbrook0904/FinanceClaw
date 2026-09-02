"""MCP-to-BaseTool adapter with a mandatory local governance overlay."""

import asyncio
import sys
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field, PrivateAttr

from financeclaw.contracts import DataClassification

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
    pass


class MCPQuoteInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class MCPQuoteTool(BaseTool):
    """Stateless local-process MCP tool exposed through the BaseTool standard."""

    name: str = "get_demo_quote"
    description: str = "Read a demo quote from the configured stateless MCP service."
    args_schema: type[BaseModel] = MCPQuoteInput

    _timeout_seconds: float = PrivateAttr(default=10.0)

    def __init__(self, *, timeout_seconds: float = 10.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    async def _delegate(self, symbol: str) -> Any:
        client = MultiServerMCPClient(
            {
                "financeclaw_demo": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "financeclaw.tools.mcp_server"],
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
        return asyncio.run(self._delegate(symbol))

    async def _arun(self, symbol: str) -> Any:
        return await self._delegate(symbol)


def managed_mcp_quote_tool(*, timeout_seconds: float = 10.0) -> ManagedTool:
    """Remote name/description/schema never alter this local governance metadata."""

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
