"""用于边界验证的模拟财经 Agent；真实财经逻辑不属于 Harness Core。"""

from .agent import MockFinanceAgent
from .plugin import MockFinanceAgentPlugin

__all__ = ["MockFinanceAgent", "MockFinanceAgentPlugin"]
