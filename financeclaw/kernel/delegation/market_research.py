"""市场研究 Agent 的领域契约，显式区分成功、提槽、不支持和部分结果。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MarketResearchInput(BaseModel):
    """可选精确范围；未提供时仍允许在有界 task 中明确研究对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 空 symbols 表示结构化范围未声明，仍需结合外层 task 判断是否提槽；
    # analysis_period 保留用户表达，不隐式补成实时行情或某个默认日期。
    symbols: tuple[str, ...] = Field(default=(), max_length=10)
    analysis_period: str | None = Field(default=None, max_length=128)


class MarketEvidence(BaseModel):
    """金融事实的来源与时点，不将模型总结视为新的证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # provider/as_of 应来自实际工具证据；as_of 保留来源时间表达，本模型只
    # 校验其存在和长度，行情新鲜度不能仅靠此 Schema 判定。
    provider: str = Field(min_length=1, max_length=128)
    as_of: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)


class MarketResearchResult(BaseModel):
    """外层运行完成不代表领域任务成功，父 Agent 必须保留 outcome 与限制。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # success 需附摘要和来源；needs_clarification 需问题及缺失字段；
    # partial/unsupported 必须保留 limitations，父 Agent 汇总时不可删除。
    outcome: Literal["success", "needs_clarification", "unsupported", "partial"]
    summary: str = Field(default="", max_length=8000)
    question: str | None = Field(default=None, max_length=2000)
    missing_fields: tuple[str, ...] = Field(default=(), max_length=20)
    evidence: tuple[MarketEvidence, ...] = Field(default=(), max_length=20)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """提槽必须有明确问题，成功必须附带行情来源与时点。"""
        if self.outcome == "needs_clarification" and (not self.question or not self.missing_fields):
            raise ValueError("clarification requires a question and missing fields")
        if self.outcome == "success" and (not self.summary or not self.evidence):
            raise ValueError("successful market research requires summary and evidence")
        if self.outcome in {"partial", "unsupported"} and not self.limitations:
            raise ValueError("partial or unsupported results must retain their limitations")
        return self
