"""长期记忆写入的治理策略：识别机密与时效性金融事实，决定敏感级别与确认要求。

评估在提案阶段执行，结论与策略版本随提案一并写入审计，供确认阶段复验。
"""

from __future__ import annotations

import re

from .models import MemoryDraft, MemorySensitivity, MemoryType

# 匹配 bearer 令牌、常见密钥前缀以及密码、API 键等机密内容的正则。
_SECRET = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b|"
    r"(?:api[_ -]?key|password|密码|口令)\s*[:=：]\s*\S+)",
    re.IGNORECASE,
)
# 匹配风险承受能力、交易授权、账户范围等高影响金融画像的正则。
_HIGH_IMPACT = re.compile(
    r"(?:risk\s*(?:tolerance|profile)|风险承受|风险偏好|financial\s+authori[sz]ation|"
    r"交易授权|账户范围|account\s+scope|broker(?:age)?\s+account|券商账户)",
    re.IGNORECASE,
)
# 匹配"当前、最新、实时"等时效性限定词的正则。
_TEMPORAL = re.compile(
    r"(?:current|currently|latest|today|now|实时|当前|最新|今日|现在|截至)",
    re.IGNORECASE,
)
# 匹配行情、持仓、汇率、宏观指标等易过期金融事实的正则。
_FINANCIAL_FACT = re.compile(
    r"(?:price|quote|holding|position|balance|cash|market\s+value|order|earnings|"
    r"valuation|exchange\s+rate|interest\s+rate|cpi|gdp|行情|股价|价格|持仓|仓位|"
    r"余额|现金|市值|订单|财报|估值|汇率|利率|宏观指标)",
    re.IGNORECASE,
)


class MemoryPolicyViolation(ValueError):
    """记忆草案被治理策略拒绝时抛出的异常。

    使用场景：
        assess 命中机密内容或时效性金融事实时抛出，调用方应把 reason
        映射为稳定错误响应，而不是将该草案写入长期记忆。

    Attributes:
        reason: 机器可读的拒绝原因码，如 secret_content_forbidden。

    """

    def __init__(self, message: str, *, reason: str) -> None:
        """保存拒绝消息与机器可读原因码。"""
        super().__init__(message)
        self.reason = reason


class MemoryPolicy:
    """长期记忆写入的确定性治理策略。

    使用场景：
        propose 与 confirm 阶段对同一草案重复评估，保证两次决策一致；
        评估结论连同版本号随提案持久化，支持审计复现。

    Attributes:
        version: 策略语义版本号，随提案与审计记录持久化。
        auto_commit_low_risk_preferences: 是否允许低风险偏好类记忆免确认
            自动提交；默认关闭，即所有写入都要求显式确认。

    """

    version = "memory-policy/1.0.0"

    def __init__(self, *, auto_commit_low_risk_preferences: bool = False) -> None:
        """初始化策略开关。

        Args:
            auto_commit_low_risk_preferences: 是否允许低风险偏好免确认自动提交。

        """
        self.auto_commit_low_risk_preferences = auto_commit_low_risk_preferences

    def assess(self, draft: MemoryDraft) -> tuple[MemorySensitivity, bool, str]:
        """评估记忆草案，返回敏感级别、是否需要显式确认与策略理由。

        Args:
            draft: 待评估的记忆草案。

        Returns:
            三元组：（敏感级别，是否需要显式确认，策略理由）。

        Raises:
            MemoryPolicyViolation: 草案包含机密内容或时效性金融事实时拒绝。

        """
        # 1. 机密与凭证内容一律禁止固化为长期记忆。
        if _SECRET.search(draft.content):
            raise MemoryPolicyViolation(
                "credentials and secrets cannot be persisted as long-term memory",
                reason="secret_content_forbidden",
            )
        # 2. 时效性金融事实必须来自受治理的工具，禁止写入长期记忆。
        if _TEMPORAL.search(draft.content) and _FINANCIAL_FACT.search(draft.content):
            raise MemoryPolicyViolation(
                "time-sensitive financial facts must be retrieved from governed tools",
                reason="temporal_financial_fact_forbidden",
            )
        # 3. 高影响金融画像提升敏感级别，并判定是否允许免确认自动提交。
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
