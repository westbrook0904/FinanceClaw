"""基于调用方持仓快照执行确定性组合风险检查的业务 Agent。"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from harness_contracts import (
    CapabilityCompletionMode,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    EgressType,
    ErrorCode,
    InvocationContext,
    RequestError,
    ResultEnvelope,
    ResultOutput,
    SideEffectType,
)
from harness_spi import AgentRequest, AgentSPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PORTFOLIO_RISK_CAPABILITY_ID = "finance.portfolio-risk/v1"

Money = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=8)]
Percentage = Annotated[Decimal, Field(ge=0, le=100, max_digits=8, decimal_places=4)]


class _Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")]
    quantity: Money
    current_price: Money
    previous_close: Money


class _RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_weight_pct: Percentage = Decimal("35")
    max_daily_loss_pct: Percentage = Decimal("3")


class _PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: Annotated[str, Field(min_length=1, max_length=128)]
    as_of: Annotated[
        str,
        Field(
            min_length=10,
            max_length=40,
            pattern=r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?$",
        ),
    ]
    base_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    cash: Money = Decimal("0")
    positions: Annotated[list[_Position], Field(min_length=1, max_length=256)]
    limits: _RiskLimits = Field(default_factory=_RiskLimits)


class PortfolioRiskAgent(AgentSPI):
    """对输入快照做可复算的风险计算，不访问外部数据或提供投资建议。"""

    _descriptor = CapabilityDescriptor(
        id=PORTFOLIO_RISK_CAPABILITY_ID,
        name="Portfolio risk review",
        type=CapabilityType.AGENT,
        version="1.0.0",
        input_schema=_PortfolioSnapshot.model_json_schema(),
        output_schema={"type": "object"},
        execution_profile=CapabilityExecutionProfile(
            side_effect=SideEffectType.NONE,
            egress=EgressType.NONE,
            completion_mode=CapabilityCompletionMode.SYNC,
        ),
        tags=frozenset({"finance", "portfolio", "risk", "deterministic", "real-use"}),
        metadata={
            "business_plugin": True,
            "data_source": "request_snapshot",
            "investment_advice": False,
            "purpose": "point-in-time portfolio valuation and limit review",
        },
    )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        try:
            snapshot = _PortfolioSnapshot.model_validate(request.input.content)
            output = _calculate(snapshot)
        except ValidationError as exc:
            error = RequestError(
                "portfolio snapshot is invalid",
                code=ErrorCode.REQUEST_INVALID,
                details={
                    "validation_error_count": exc.error_count(),
                    "capability_id": self._descriptor.id,
                },
            )
            return ResultEnvelope.failure(error.to_detail())
        except ArithmeticError as exc:
            error = RequestError(
                "portfolio snapshot cannot be valued",
                code=ErrorCode.REQUEST_INVALID,
                details={
                    "cause_type": type(exc).__name__,
                    "capability_id": self._descriptor.id,
                },
            )
            return ResultEnvelope.failure(error.to_detail())

        return ResultEnvelope.success(
            ResultOutput(type="portfolio_risk_review", data=output),
            metadata={
                "capability_id": self._descriptor.id,
                "request_id": context.request.request_id,
                "data_source": "request_snapshot",
                "calculation_version": "portfolio-risk-v1",
            },
        )


def _calculate(snapshot: _PortfolioSnapshot) -> dict[str, object]:
    positions: list[dict[str, object]] = []
    current_positions_value = sum(
        (item.quantity * item.current_price for item in snapshot.positions),
        start=Decimal("0"),
    )
    previous_positions_value = sum(
        (item.quantity * item.previous_close for item in snapshot.positions),
        start=Decimal("0"),
    )
    net_asset_value = snapshot.cash + current_positions_value
    previous_net_asset_value = snapshot.cash + previous_positions_value
    if net_asset_value <= 0 or previous_net_asset_value <= 0:
        raise ArithmeticError("portfolio net asset value must be positive")

    daily_pnl = net_asset_value - previous_net_asset_value
    daily_return_pct = daily_pnl / previous_net_asset_value * Decimal("100")
    breaches: list[dict[str, object]] = []

    for item in sorted(snapshot.positions, key=lambda value: value.symbol):
        market_value = item.quantity * item.current_price
        position_pnl = item.quantity * (item.current_price - item.previous_close)
        weight_pct = market_value / net_asset_value * Decimal("100")
        positions.append(
            {
                "symbol": item.symbol,
                "quantity": _decimal(item.quantity, 8),
                "market_value": _decimal(market_value, 2),
                "daily_pnl": _decimal(position_pnl, 2),
                "weight_pct": _decimal(weight_pct, 4),
            }
        )
        if weight_pct > snapshot.limits.max_position_weight_pct:
            breaches.append(
                {
                    "code": "POSITION_CONCENTRATION",
                    "severity": "warning",
                    "symbol": item.symbol,
                    "actual_pct": _decimal(weight_pct, 4),
                    "limit_pct": _decimal(snapshot.limits.max_position_weight_pct, 4),
                }
            )

    if daily_return_pct < -snapshot.limits.max_daily_loss_pct:
        breaches.append(
            {
                "code": "DAILY_LOSS_LIMIT",
                "severity": "critical",
                "actual_pct": _decimal(daily_return_pct, 4),
                "limit_pct": _decimal(snapshot.limits.max_daily_loss_pct, 4),
            }
        )

    risk_level: Literal["low", "medium", "high"]
    if any(item["severity"] == "critical" for item in breaches):
        risk_level = "high"
    elif breaches:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "portfolio_id": snapshot.portfolio_id,
        "as_of": snapshot.as_of,
        "base_currency": snapshot.base_currency,
        "valuation": {
            "cash": _decimal(snapshot.cash, 2),
            "positions_value": _decimal(current_positions_value, 2),
            "net_asset_value": _decimal(net_asset_value, 2),
            "daily_pnl": _decimal(daily_pnl, 2),
            "daily_return_pct": _decimal(daily_return_pct, 4),
        },
        "positions": positions,
        "breaches": breaches,
        "risk_level": risk_level,
        "grounding": {
            "source": "request_snapshot",
            "position_count": len(snapshot.positions),
            "calculation_version": "portfolio-risk-v1",
        },
        "disclaimer": "Point-in-time risk calculation; not investment advice.",
    }


def _decimal(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")
