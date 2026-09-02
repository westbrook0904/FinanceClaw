"""First production-shaped local READ and WRITE BaseTool examples."""

import json
from decimal import Decimal
from typing import Any, Literal

from langchain_core.tools import BaseTool
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
from .policy import TransientToolError


class MarketSnapshotInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class WatchlistInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    note: str = Field(default="", max_length=200)


class CalculatorInput(BaseModel):
    operation: Literal["add", "subtract", "multiply", "divide"]
    left: float
    right: float


class MarketSnapshotTool(BaseTool):
    name: str = "market_snapshot"
    description: str = "Read a bounded market snapshot with provider and as-of evidence."
    args_schema: type[BaseModel] = MarketSnapshotInput

    _remaining_failures: int = PrivateAttr(default=0)
    _call_count: int = PrivateAttr(default=0)

    def __init__(self, *, fail_first: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._remaining_failures = fail_first

    @property
    def call_count(self) -> int:
        return self._call_count

    def _run(self, symbol: str) -> str:
        self._call_count += 1
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise TransientToolError("market data provider is temporarily unavailable")
        return json.dumps(
            {
                "symbol": symbol.upper(),
                "price": "100.00",
                "currency": "USD",
                "provider": "financeclaw-stage1-demo",
                "as_of": "2026-09-02T00:00:00Z",
                "fallback_used": False,
            },
            sort_keys=True,
        )

    async def _arun(self, symbol: str) -> str:
        return self._run(symbol)


class WatchlistWriteTool(BaseTool):
    name: str = "watchlist_add"
    description: str = "Add a symbol to the authenticated subject's watchlist."
    args_schema: type[BaseModel] = WatchlistInput

    _writes: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def writes(self) -> tuple[dict[str, str], ...]:
        return tuple(self._writes)

    def _run(self, symbol: str, note: str = "") -> str:
        record = {"symbol": symbol.upper(), "note": note}
        self._writes.append(record)
        return json.dumps({"status": "written", **record}, sort_keys=True)

    async def _arun(self, symbol: str, note: str = "") -> str:
        return self._run(symbol, note)


class CalculatorTool(BaseTool):
    """Migration of the useful deterministic calculator without PluginSPI."""

    name: str = "calculate"
    description: str = "Perform deterministic arithmetic on two numbers."
    args_schema: type[BaseModel] = CalculatorInput

    def _run(self, operation: str, left: float, right: float) -> str:
        values = {"left": Decimal(str(left)), "right": Decimal(str(right))}
        if operation == "add":
            result = values["left"] + values["right"]
        elif operation == "subtract":
            result = values["left"] - values["right"]
        elif operation == "multiply":
            result = values["left"] * values["right"]
        elif operation == "divide":
            if values["right"] == 0:
                raise ValueError("calculator cannot divide by zero")
            result = values["left"] / values["right"]
        else:  # pragma: no cover - Pydantic rejects this before execution
            raise ValueError("unsupported calculator operation")
        return json.dumps({"value": format(result, "f")}, sort_keys=True)

    async def _arun(self, operation: str, left: float, right: float) -> str:
        return self._run(operation, left, right)


def default_local_tools(
    *,
    market_tool: MarketSnapshotTool | None = None,
    write_tool: WatchlistWriteTool | None = None,
) -> tuple[ManagedTool, ...]:
    common_data_classes = frozenset(
        {DataClassification.PUBLIC, DataClassification.INTERNAL, DataClassification.CONFIDENTIAL}
    )
    return (
        ManagedTool(
            market_tool or MarketSnapshotTool(),
            ToolGovernance(
                tool_id="market_snapshot",
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
                allowed_data_classes=common_data_classes,
            ),
        ),
        ManagedTool(
            write_tool or WatchlistWriteTool(),
            ToolGovernance(
                tool_id="watchlist_add",
                version="1.0.0",
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.KEY_REQUIRED,
                risk_level=RiskLevel.MEDIUM,
                required_scopes=frozenset({"watchlist:write"}),
                approval=ApprovalMode.ALWAYS,
                egress=Egress.INTERNAL,
                sensitivity=Sensitivity.CONFIDENTIAL,
                retry_profile=RetryProfile.NONE,
                audit_level=AuditLevel.FULL,
                allowed_data_classes=common_data_classes,
            ),
        ),
        ManagedTool(
            CalculatorTool(),
            ToolGovernance(
                tool_id="calculate",
                version="1.0.0",
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.LOW,
                required_scopes=frozenset({"tools:read"}),
                approval=ApprovalMode.NONE,
                egress=Egress.NONE,
                sensitivity=Sensitivity.INTERNAL,
                retry_profile=RetryProfile.NONE,
                audit_level=AuditLevel.EXECUTION,
            ),
        ),
    )
