"""本地金融 Tool 的参考实现：行情快照、自选股写入与确定性计算器。

属于 orchestration/tools 治理层的实现模块，每个 Tool 都与治理元数据
一起包装为 ManagedTool，供编排层装配进 ToolCatalog 与 Agent 工具集。
"""

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
    """market_snapshot Tool 的入参模型：单个证券代码。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema。

    Attributes:
        symbol: 证券代码，1~16 位，仅允许字母、数字与 ``. _ -``。

    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class WatchlistInput(BaseModel):
    """watchlist_add Tool 的入参模型：待加入自选股的证券与备注。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema。

    Attributes:
        symbol: 证券代码，1~16 位，仅允许字母、数字与 ``. _ -``。
        note: 可选备注，最长 200 字符，默认为空字符串。

    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    note: str = Field(default="", max_length=200)


class CalculatorInput(BaseModel):
    """calculate Tool 的入参模型：二元算术运算请求。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema。

    Attributes:
        operation: 运算类型，限定为四则运算之一。
        left: 左操作数。
        right: 右操作数；除法时不得为 0。

    """

    operation: Literal["add", "subtract", "multiply", "divide"]
    left: float
    right: float


class MarketSnapshotTool(BaseTool):
    """读取带来源与时间戳证据的行情快照 Tool（阶段一演示数据）。

    使用场景：供具备 market:read 作用域的行情类 Agent 调用；输出
    JSON 字符串，包含证券代码、价格、币种、数据提供方与 as-of 时间戳，
    供上层做来源与时效标注。治理上配置为瞬态只读，依赖短暂故障时
    抛出 TransientToolError 触发自动重试。

    Attributes:
        name: Tool 名称，固定为 ``market_snapshot``，与治理 tool_id 一致。
        description: 展示给 Agent 的工具用途说明。
        args_schema: 入参模型，见 MarketSnapshotInput。
        _remaining_failures: 测试用故障注入计数，大于 0 时前 N 次
            调用抛出瞬态错误。
        _call_count: 实际执行次数统计，供测试断言重试行为。

    """

    name: str = "market_snapshot"
    description: str = "Read a bounded market snapshot with provider and as-of evidence."
    args_schema: type[BaseModel] = MarketSnapshotInput

    _remaining_failures: int = PrivateAttr(default=0)
    _call_count: int = PrivateAttr(default=0)

    def __init__(self, *, fail_first: int = 0, **kwargs: Any) -> None:
        """初始化行情快照 Tool。

        Args:
            fail_first: 故障注入次数；前 N 次调用抛出瞬态错误以模拟
                依赖故障，默认 0 表示不注入。
            **kwargs: 透传给 ``BaseTool`` 的其余字段。

        """
        super().__init__(**kwargs)
        self._remaining_failures = fail_first

    @property
    def call_count(self) -> int:
        """返回该 Tool 已被实际执行的次数，供测试断言重试行为。"""
        return self._call_count

    def _run(self, symbol: str) -> str:
        """执行一次快照读取，返回带证据字段的 JSON 快照。

        Args:
            symbol: 证券代码。

        Returns:
            按键排序的 JSON 字符串，包含价格、币种、提供方与 as-of 时间。

        Raises:
            TransientToolError: 故障注入未耗尽时模拟依赖暂时不可用。

        """
        # 1. 统计执行次数；处于故障注入期时抛出瞬态错误以触发重试。
        self._call_count += 1
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise TransientToolError("market data provider is temporarily unavailable")
        # 2. 返回阶段一演示快照，附带提供方与 as-of 证据字段。
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
        """异步入口：直接复用同步实现，保证两侧行为一致。"""
        return self._run(symbol)


class WatchlistWriteTool(BaseTool):
    """向已认证主体的自选股列表写入证券 Tool（写类，需人工审批）。

    使用场景：供具备 watchlist:write 作用域的 Agent 在用户批准后调用；
    每次调用记录一条 (symbol, note) 写入，输出 JSON 确认结果。治理上
    标记为 WRITE 且 ALWAYS 审批，执行前会经过人工二次授权。

    Attributes:
        name: Tool 名称，固定为 ``watchlist_add``，与治理 tool_id 一致。
        description: 展示给 Agent 的工具用途说明。
        args_schema: 入参模型，见 WatchlistInput。
        _writes: 本次实例累计的写入记录列表，供测试断言写入内容。

    """

    name: str = "watchlist_add"
    description: str = "Add a symbol to the authenticated subject's watchlist."
    args_schema: type[BaseModel] = WatchlistInput

    _writes: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def writes(self) -> tuple[dict[str, str], ...]:
        """返回累计写入记录的只读视图，供测试断言写入行为。"""
        return tuple(self._writes)

    def _run(self, symbol: str, note: str = "") -> str:
        """执行一次自选股写入，返回 JSON 确认结果。

        Args:
            symbol: 证券代码，写入前统一转为大写。
            note: 可选备注，原样写入。

        Returns:
            按键排序的 JSON 字符串，包含写入状态与最终记录内容。

        """
        record = {"symbol": symbol.upper(), "note": note}
        self._writes.append(record)
        return json.dumps({"status": "written", **record}, sort_keys=True)

    async def _arun(self, symbol: str, note: str = "") -> str:
        """异步入口：直接复用同步实现，保证两侧行为一致。"""
        return self._run(symbol, note)


class CalculatorTool(BaseTool):
    """对两个数执行确定性四则运算的 Tool（纯计算，无外部访问）。

    使用场景：供各类 Agent 做安全、可复现的算术求值；内部用 Decimal
    计算避免二进制浮点误差，输出 JSON 结果。治理上标记为只读且无出域。

    Attributes:
        name: Tool 名称，固定为 ``calculate``，与治理 tool_id 一致。
        description: 展示给 Agent 的工具用途说明。
        args_schema: 入参模型，见 CalculatorInput。

    """

    name: str = "calculate"
    description: str = "Perform deterministic arithmetic on two numbers."
    args_schema: type[BaseModel] = CalculatorInput

    def _run(self, operation: str, left: float, right: float) -> str:
        """执行一次四则运算，返回 JSON 格式的精确结果。

        Args:
            operation: 运算类型，见 CalculatorInput.operation。
            left: 左操作数。
            right: 右操作数。

        Returns:
            按键排序的 JSON 字符串，仅包含 ``value`` 一个结果字段。

        Raises:
            ValueError: 除数为 0 或运算类型不受支持。

        """
        # 1. 用 Decimal 承载操作数，避免浮点误差，保证结果确定性。
        values = {"left": Decimal(str(left)), "right": Decimal(str(right))}
        # 2. 按运算类型分派；除法前显式检查除数不为 0。
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
        else:
            raise ValueError("unsupported calculator operation")
        return json.dumps({"value": format(result, "f")}, sort_keys=True)

    async def _arun(self, operation: str, left: float, right: float) -> str:
        """异步入口：直接复用同步实现，保证两侧行为一致。"""
        return self._run(operation, left, right)


def default_local_tools(
    *,
    market_tool: MarketSnapshotTool | None = None,
    write_tool: WatchlistWriteTool | None = None,
) -> tuple[ManagedTool, ...]:
    """装配默认的本地 Tool 集合：行情快照、自选股写入与计算器。

    使用场景：编排层初始化受治理 Tool 目录时调用；测试可通过参数
    注入定制的行情或写入 Tool 实例，治理元数据保持不变。

    Args:
        market_tool: 可选的定制行情快照 Tool；缺省时新建默认实例。
        write_tool: 可选的定制自选股写入 Tool；缺省时新建默认实例。

    Returns:
        三个 ManagedTool 组成的元组，顺序为 market_snapshot、
        watchlist_add、calculate，治理配置逐项对应。

    """
    # 1. 三个 Tool 共用的数据密级范围：公开、内部与机密。
    common_data_classes = frozenset(
        {DataClassification.PUBLIC, DataClassification.INTERNAL, DataClassification.CONFIDENTIAL}
    )
    return (
        # 2. 行情快照：只读、瞬态可重试、需 market:read 作用域。
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
        # 3. 自选股写入：写类、强制人工审批、需 watchlist:write 作用域。
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
        # 4. 计算器：只读、无出域、无需审批，审计仅记录执行。
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
