"""根据记忆类型、敏感度和来源决定确认与写入策略。"""

from __future__ import annotations

import re

from .models import MemoryDraft, MemorySensitivity, MemoryType

_SECRET = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b|"
    r"(?:api[_ -]?key|password|密码|口令)\s*[:=：]\s*\S+)",
    re.IGNORECASE,
)
_HIGH_IMPACT = re.compile(
    r"(?:risk\s*(?:tolerance|profile)|风险承受|风险偏好|financial\s+authori[sz]ation|"
    r"交易授权|账户范围|account\s+scope|broker(?:age)?\s+account|券商账户)",
    re.IGNORECASE,
)
_TEMPORAL = re.compile(
    r"(?:current|currently|latest|today|now|实时|当前|最新|今日|现在|截至)",
    re.IGNORECASE,
)
_FINANCIAL_FACT = re.compile(
    r"(?:price|quote|holding|position|balance|cash|market\s+value|order|earnings|"
    r"valuation|exchange\s+rate|interest\s+rate|cpi|gdp|行情|股价|价格|持仓|仓位|"
    r"余额|现金|市值|订单|财报|估值|汇率|利率|宏观指标)",
    re.IGNORECASE,
)


class MemoryPolicyViolation(ValueError):
    """定义记忆策略Violation。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        reason: 产生当前决策、遗漏或状态的可读原因。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        """注入并保存记忆策略Violation所需的协作对象，同时校验构造期不变量。"""
        super().__init__(message)
        self.reason = reason


class MemoryPolicy:
    """把记忆候选分类为自动提交、需确认或拒绝。

    适用场景：
        用于在执行副作用前作出确定性治理决策。

    属性：
        version: 语义版本，用于固定运行行为并支持审计复现。
        auto_commit_low_risk_preferences: 是否允许有直接用户证据的低风险偏好自动成为长期记忆。
    """

    version = "memory-policy/1.0.0"

    def __init__(self, *, auto_commit_low_risk_preferences: bool = False) -> None:
        """注入并保存记忆策略所需的协作对象，同时校验构造期不变量。"""
        self.auto_commit_low_risk_preferences = auto_commit_low_risk_preferences

    def assess(self, draft: MemoryDraft) -> tuple[MemorySensitivity, bool, str]:
        """根据记忆类型、敏感度和证据来源决定拒绝、确认或自动提交。"""
        if _SECRET.search(draft.content):
            raise MemoryPolicyViolation(
                "credentials and secrets cannot be persisted as long-term memory",
                reason="secret_content_forbidden",
            )
        if _TEMPORAL.search(draft.content) and _FINANCIAL_FACT.search(draft.content):
            raise MemoryPolicyViolation(
                "time-sensitive financial facts must be retrieved from governed tools",
                reason="temporal_financial_fact_forbidden",
            )
        high_impact = bool(_HIGH_IMPACT.search(draft.content))
        sensitivity = MemorySensitivity.CONFIDENTIAL if high_impact else MemorySensitivity.INTERNAL
        can_auto_commit = (
            self.auto_commit_low_risk_preferences
            and draft.kind is MemoryType.PREFERENCE
            and not high_impact
        )
        if can_auto_commit:
            return sensitivity, False, "explicit low-risk preference auto-commit is enabled"
        if high_impact:
            return sensitivity, True, "high-impact financial profile requires explicit approval"
        return sensitivity, True, "long-term memory writes require explicit approval"
