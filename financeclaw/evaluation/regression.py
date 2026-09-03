"""提供 regression 评测与发布能力。"""

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

REQUIRED_CATEGORIES = frozenset(
    {
        "tool_selection",
        "slash_slots",
        "delegation",
        "policy",
        "context_memory",
        "financial_freshness",
        "workflow_recovery",
        "prompt_injection",
        "cross_tenant",
        "provider_failure",
    }
)


class RegressionCase(BaseModel):
    """定义RegressionCase。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        case_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        category: 回归用例所属类别，便于分组统计和门禁定位。
        critical: 该用例失败是否必须立即阻止发布。
        inputs: 执行评测用例所需的结构化输入。
        reference_outputs: 评测时用于比较的预期关键输出。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    critical: bool = False
    inputs: dict[str, Any]
    reference_outputs: dict[str, Any]


class EvaluationResult(BaseModel):
    """定义评测的执行结果。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        case_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        passed: 该用例是否满足验收条件。
        score: 评测得分，通常归一化到 0 至 1。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    score: float = Field(ge=0, le=1)


class RegressionGate:
    """核对评测用例与结果，阻止缺失、失败或低于阈值的发布。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        minimum_score: 非关键用例汇总后允许通过门禁的最低得分。
    """

    def __init__(self, *, minimum_score: float = 0.95) -> None:
        """注入并保存RegressionGate所需的协作对象，同时校验构造期不变量。"""
        if not 0 < minimum_score <= 1:
            raise ValueError("minimum_score must be in (0, 1]")
        self.minimum_score = minimum_score

    def assert_passed(
        self,
        cases: tuple[RegressionCase, ...],
        results: tuple[EvaluationResult, ...],
    ) -> None:
        """校验结果覆盖所有用例、关键用例全部通过且总体得分达到门槛。"""
        categories = {case.category for case in cases}
        missing = REQUIRED_CATEGORIES - categories
        if missing:
            raise AssertionError(f"regression dataset is missing categories: {sorted(missing)}")
        by_id = {result.case_id: result for result in results}
        if set(by_id) != {case.case_id for case in cases}:
            raise AssertionError("evaluation results do not match the versioned dataset")
        failed_critical = [
            case.case_id for case in cases if case.critical and not by_id[case.case_id].passed
        ]
        if failed_critical:
            raise AssertionError(f"critical evaluation failures: {failed_critical}")
        average = sum(result.score for result in results) / len(results)
        if average < self.minimum_score:
            raise AssertionError(
                f"evaluation score {average:.3f} is below baseline {self.minimum_score:.3f}"
            )


def load_cases(path: str | Path) -> tuple[RegressionCase, ...]:
    """从持久化表示加载并校验regression 模块的数据。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(RegressionCase.model_validate(item) for item in raw["cases"])


class _LangSmithClient(Protocol):
    """定义LangSmithClient。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    def create_dataset(self, *, dataset_name: str, description: str) -> Any:
        """创建并返回新的LangSmithClient。"""
        ...

    def create_examples(self, *, dataset_id: Any, examples: list[dict[str, Any]]) -> Any:
        """创建并返回新的LangSmithClient。"""
        ...


def publish_cases(
    client: _LangSmithClient,
    *,
    dataset_name: str,
    cases: tuple[RegressionCase, ...],
) -> Any:
    """发布regression 模块的数据并返回供应方结果。"""
    dataset = client.create_dataset(
        dataset_name=dataset_name,
        description="FinanceClaw Stage-5 production security and behavior regression gate",
    )
    client.create_examples(
        dataset_id=dataset.id,
        examples=[
            {
                "inputs": case.inputs,
                "outputs": case.reference_outputs,
                "metadata": {
                    "case_id": case.case_id,
                    "category": case.category,
                    "critical": case.critical,
                },
            }
            for case in cases
        ],
    )
    return dataset
