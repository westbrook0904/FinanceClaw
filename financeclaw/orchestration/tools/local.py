"""提供开发、测试和离线运行所需的本地金融工具。"""

import json
from decimal import Decimal
from typing import Any, Literal

from langchain_core.tools import BaseTool
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
from .policy import TransientToolError


class MarketSnapshotInput(BaseModel):
    """定义行情Snapshot的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        symbol: 标准化金融标的代码。
    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class WatchlistInput(BaseModel):
    """定义观察列表的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        symbol: 标准化金融标的代码。
        note: 写入观察列表时附带的用户备注。
    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    note: str = Field(default="", max_length=200)


class CalculatorInput(BaseModel):
    """定义Calculator的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        operation: 计算器允许执行的运算名称。
        left: 二元运算左操作数。
        right: 二元运算右操作数。
    """

    operation: Literal["add", "subtract", "multiply", "divide"]
    left: float
    right: float


class MarketSnapshotTool(BaseTool):
    """定义行情Snapshot工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
        _remaining_failures: 内部 `remaining failures` 状态或依赖，不属于公开接口。
        _call_count: 内部 `call count` 状态或依赖，不属于公开接口。
    """

    name: str = "market_snapshot"
    description: str = "Read a bounded market snapshot with provider and as-of evidence."
    args_schema: type[BaseModel] = MarketSnapshotInput

    _remaining_failures: int = PrivateAttr(default=0)
    _call_count: int = PrivateAttr(default=0)

    def __init__(self, *, fail_first: int = 0, **kwargs: Any) -> None:
        """注入并保存行情Snapshot工具所需的协作对象，同时校验构造期不变量。"""
        super().__init__(**kwargs)
        self._remaining_failures = fail_first

    @property
    def call_count(self) -> int:
        """返回测试行情工具累计被调用的次数。"""
        return self._call_count

    def _run(self, symbol: str) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
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
        """执行工具的异步实现，保持与同步入口相同的业务语义。"""
        return self._run(symbol)


class WatchlistWriteTool(BaseTool):
    """定义观察列表Write工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
        _writes: 内部 `writes` 状态或依赖，不属于公开接口。
    """

    name: str = "watchlist_add"
    description: str = "Add a symbol to the authenticated subject's watchlist."
    args_schema: type[BaseModel] = WatchlistInput

    _writes: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def writes(self) -> tuple[dict[str, str], ...]:
        """返回测试写工具已接收记录的不可变快照。"""
        return tuple(self._writes)

    def _run(self, symbol: str, note: str = "") -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
        record = {"symbol": symbol.upper(), "note": note}
        self._writes.append(record)
        return json.dumps({"status": "written", **record}, sort_keys=True)

    async def _arun(self, symbol: str, note: str = "") -> str:
        """执行工具的异步实现，保持与同步入口相同的业务语义。"""
        return self._run(symbol, note)


class CalculatorTool(BaseTool):
    """定义Calculator工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
    """

    name: str = "calculate"
    description: str = "Perform deterministic arithmetic on two numbers."
    args_schema: type[BaseModel] = CalculatorInput

    def _run(self, operation: str, left: float, right: float) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
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
        """执行工具的异步实现，保持与同步入口相同的业务语义。"""
        return self._run(operation, left, right)


def default_local_tools(
    *,
    market_tool: MarketSnapshotTool | None = None,
    write_tool: WatchlistWriteTool | None = None,
) -> tuple[ManagedTool, ...]:
    """构造开发与测试使用的行情读取、观察列表写入和计算器工具集合。"""
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
