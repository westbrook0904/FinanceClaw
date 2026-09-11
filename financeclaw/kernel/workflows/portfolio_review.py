"""组合复盘的版本化输入、输出及稳定标识。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel.responses import ArtifactReference

WORKFLOW_ID = "portfolio_review"


WORKFLOW_VERSION = "1.1.0"


ASSISTANT_ID = "portfolio_review_v1_1_0"


APPROVAL_POINT = "publish_portfolio_report"


MARKET_TOOL_ID = "market_snapshot"


MARKET_TOOL_VERSION = "1.0.0"


class _FrozenModel(BaseModel):
    """流程契约模型的内部基类：冻结、禁止多余字段且拒绝 NaN/Inf。

    使用场景：本模块全部 Pydantic 契约模型统一继承，保证输入输出
    不可变、结构严格，杜绝隐式字段注入与非法数值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PortfolioPosition(_FrozenModel):
    """单个持仓的输入契约：证券代码、数量与成本。

    使用场景：作为 ``PortfolioReviewInput.positions`` 的元素，经 Pydantic
    严格校验后参与市值与集中度的确定性计算。

    Attributes:
        symbol: 证券代码，1 到 16 字符，仅允许字母、数字与 ``.`` ``_`` ``-``。
        quantity: 持仓数量，正数，最多 24 位数字、8 位小数。
        cost_basis: 持仓成本，非负数，精度约束与数量一致。

    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    cost_basis: Decimal = Field(ge=0, max_digits=24, decimal_places=8)


class PortfolioReviewInput(_FrozenModel):
    """portfolio_review@1.0.0 的输入契约。

    使用场景：作为图的 input_schema，在 normalize_input 节点完成校验
    与 JSON 化归一化，并派生后续审批与审计共用的入参哈希。

    Attributes:
        portfolio_name: 组合名称，1 到 120 字符。
        positions: 持仓列表，1 到 20 条，证券代码大小写不敏感地唯一。
        max_snapshot_age_hours: 允许的行情快照最长大龄（小时），默认 48，取值 1 到 168。

    """

    portfolio_name: str = Field(min_length=1, max_length=120)
    positions: tuple[PortfolioPosition, ...] = Field(min_length=1, max_length=20)
    max_snapshot_age_hours: int = Field(default=48, ge=1, le=168)

    @model_validator(mode="after")
    def symbols_must_be_unique(self) -> PortfolioReviewInput:
        """校验持仓的证券代码大小写不敏感地不重复。

        Raises:
            ValueError: 存在重复证券代码时抛出。

        """
        symbols = tuple(item.symbol.upper() for item in self.positions)
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio positions must have unique symbols")
        return self


class PortfolioSourceReference(_FrozenModel):
    """单条行情来源引用：记录产出计算输入的快照溯源信息。

    使用场景：finalize 阶段由快照投影而来，随输出返回，便于审计与
    复核每笔行情的提供方、时点与版本。

    Attributes:
        symbol: 该快照对应的证券代码（已归一为大写）。
        provider: 行情提供方标识。
        as_of: 快照时点（ISO 格式，带时区）。
        input_hash: 该次行情调用的规范入参哈希（64 位小写十六进制）。
        tool_version: 产出快照的工具版本。

    """

    symbol: str
    provider: str
    as_of: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_version: str


class PortfolioReviewOutput(_FrozenModel):
    """portfolio_review@1.0.0 的输出契约（终态结果）。

    使用场景：finalize 节点构造并返回，经 output_schema 校验后成为图
    输出；校验器保证终态自洽——completed 必带制品，非 completed 必带
    错误信息。

    Attributes:
        workflow_id: 工作流标识，固定为 portfolio_review。
        workflow_version: 工作流版本，固定为 1.0.0。
        turn_id: 本次运行的唯一 ID。
        status: 终态：completed（已发布报告）、rejected（审批驳回）或 failed。
        arguments_hash: 归一化输入的规范哈希，串联审计与审批比对。
        portfolio_name: 组合名称（回显输入）。
        snapshot_as_of: 全组合最旧快照时点；未通过新鲜度校验时为 None。
        total_market_value: 组合总市值（两位小数字符串）；未完成分析时为 None。
        largest_position_weight: 最大单一持仓权重（四位小数字符串）；未完成分析时为 None。
        risk_band: 集中度风险档（low/moderate/high）；未完成分析时为 None。
        source_refs: 行情来源引用列表；无可用快照时为空元组。
        artifact: 已发布报告的制品引用；仅 completed 时非 None。
        error: 失败或驳回原因；completed 时为 None。

    """

    workflow_id: Literal["portfolio_review"]
    workflow_version: Literal["1.1.0"]
    turn_id: str
    status: Literal["completed", "rejected", "failed"]
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_name: str
    snapshot_as_of: datetime | None = None
    total_market_value: str | None = None
    largest_position_weight: str | None = None
    risk_band: Literal["low", "moderate", "high"] | None = None
    source_refs: tuple[PortfolioSourceReference, ...] = ()
    artifact: ArtifactReference | None = None
    error: str | None = None

    @model_validator(mode="after")
    def terminal_shape_matches_status(self) -> PortfolioReviewOutput:
        """校验终态自洽：completed 必带制品，非 completed 必带错误信息。

        Raises:
            ValueError: 终态与制品、错误信息不匹配时抛出。

        """
        if self.status == "completed" and self.artifact is None:
            raise ValueError("completed portfolio review requires a report artifact")
        if self.status != "completed" and not self.error:
            raise ValueError("non-completed portfolio review requires an error")
        return self
