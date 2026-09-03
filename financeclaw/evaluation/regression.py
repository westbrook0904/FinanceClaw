"""离线回归评测的核心实现：用例契约、加载、发布门禁与 LangSmith 发布。

属于 evaluation 层：``RegressionCase`` 与 ``EvaluationResult`` 定义回归用例和
评测结果的稳定数据契约；``load_cases`` 从版本库 JSON 加载用例；
``RegressionGate`` 按"类别覆盖、结果对齐、关键用例、平均分基线"执行发布
门禁；``publish_cases`` 把用例发布为带版本名的 LangSmith 回归数据集。
"""

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

# 发布门禁要求数据集必须覆盖的用例类别：工具、补槽、委派、策略、记忆、
# 金融时效、恢复、注入、租户与 Provider 故障。
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
    """单条离线回归用例：绑定类别与关键性标记的输入和参考输出。

    使用场景：``load_cases`` 从版本库 JSON 反序列化得到；作为发布门禁
    ``RegressionGate.assert_passed`` 的评分对象，并经 ``publish_cases``
    写入 LangSmith 数据集。

    Attributes:
        case_id: 用例全局唯一 ID，用于把评测结果对齐回用例。
        category: 用例所属类别，用于门禁检查类别覆盖（见 ``REQUIRED_CATEGORIES``）。
        critical: 是否为关键用例；关键用例失败将直接导致门禁失败。
        inputs: 发给被测系统的输入负载（如用户消息与上下文）。
        reference_outputs: 评分时对照的参考输出负载。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    critical: bool = False
    inputs: dict[str, Any]
    reference_outputs: dict[str, Any]


class EvaluationResult(BaseModel):
    """单条用例的评测结果：是否通过与 [0, 1] 区间内的归一化得分。

    使用场景：由评测执行方对每个 ``RegressionCase`` 产出，成批传入
    ``RegressionGate.assert_passed`` 参与门禁判定。

    Attributes:
        case_id: 对应的回归用例 ID。
        passed: 该用例是否通过评测。
        score: 归一化得分，取值范围 [0, 1]。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    score: float = Field(ge=0, le=1)


class RegressionGate:
    """发布门禁：校验类别覆盖、结果对齐、关键用例与平均分基线。

    使用场景：发布前把数据集用例与评测结果交给 ``assert_passed``；
    任一检查不满足即抛出 ``AssertionError`` 使发布流程失败。

    Attributes:
        minimum_score: 全量用例平均分必须达到的基线，取值范围 (0, 1]。

    """

    def __init__(self, *, minimum_score: float = 0.95) -> None:
        """校验并保存平均分基线。

        Args:
            minimum_score: 平均分基线，默认 0.95。

        Raises:
            ValueError: minimum_score 不在 (0, 1] 区间内。

        """
        if not 0 < minimum_score <= 1:
            raise ValueError("minimum_score must be in (0, 1]")
        self.minimum_score = minimum_score

    def assert_passed(
        self,
        cases: tuple[RegressionCase, ...],
        results: tuple[EvaluationResult, ...],
    ) -> None:
        """执行发布门禁：类别覆盖、结果对齐、关键用例与平均分逐项校验。

        Args:
            cases: 版本库数据集中的全部回归用例。
            results: 与用例一一对应的评测结果。

        Raises:
            AssertionError: 数据集缺少必备类别、评测结果与用例不对齐、
                存在失败的关键用例，或平均分低于基线。

        """
        # 1. 校验数据集覆盖全部必备类别。
        categories = {case.category for case in cases}
        missing = REQUIRED_CATEGORIES - categories
        if missing:
            raise AssertionError(f"regression dataset is missing categories: {sorted(missing)}")
        # 2. 校验评测结果与用例按 case_id 一一对齐。
        by_id = {result.case_id: result for result in results}
        if set(by_id) != {case.case_id for case in cases}:
            raise AssertionError("evaluation results do not match the versioned dataset")
        # 3. 校验关键用例全部通过，任一失败即门禁失败。
        failed_critical = [
            case.case_id for case in cases if case.critical and not by_id[case.case_id].passed
        ]
        if failed_critical:
            raise AssertionError(f"critical evaluation failures: {failed_critical}")
        # 4. 校验全量用例平均分不低于基线。
        average = sum(result.score for result in results) / len(results)
        if average < self.minimum_score:
            raise AssertionError(
                f"evaluation score {average:.3f} is below baseline {self.minimum_score:.3f}"
            )


def load_cases(path: str | Path) -> tuple[RegressionCase, ...]:
    """从版本库 JSON 文件加载全部回归用例。

    Args:
        path: 回归集 JSON 文件路径，顶层需包含 ``cases`` 数组。

    Returns:
        按文件顺序排列的回归用例元组。

    Raises:
        pydantic.ValidationError: 用例字段不符合 ``RegressionCase`` 契约。

    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(RegressionCase.model_validate(item) for item in raw["cases"])


class _LangSmithClient(Protocol):
    """``publish_cases`` 依赖的最小 LangSmith 客户端协议。

    使用场景：评测与测试可注入满足该协议的内存假客户端，避免依赖真实
    LangSmith 服务；真实场景直接传入 ``langsmith.Client``。

    """

    def create_dataset(self, *, dataset_name: str, description: str) -> Any:
        """在 LangSmith 上创建一个指定名称与描述的回归数据集。"""

    def create_examples(self, *, dataset_id: Any, examples: list[dict[str, Any]]) -> Any:
        """向指定数据集批量写入评测样例。"""


def publish_cases(
    client: _LangSmithClient,
    *,
    dataset_name: str,
    cases: tuple[RegressionCase, ...],
) -> Any:
    """在 LangSmith 创建回归数据集并灌入全部用例。

    Args:
        client: 满足 ``_LangSmithClient`` 协议的 LangSmith 客户端。
        dataset_name: 目标数据集名，约定包含版本号，改版时启用新名字。
        cases: 待发布的回归用例。

    Returns:
        新建数据集对象（含 ``id`` 与 ``name``）。

    """
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
