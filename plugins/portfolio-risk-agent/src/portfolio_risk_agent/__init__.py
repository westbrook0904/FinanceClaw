"""真实组合风险检查业务插件。"""

from .agent import PORTFOLIO_RISK_CAPABILITY_ID, PortfolioRiskAgent
from .plugin import PortfolioRiskAgentPlugin

__all__ = [
    "PORTFOLIO_RISK_CAPABILITY_ID",
    "PortfolioRiskAgent",
    "PortfolioRiskAgentPlugin",
]
