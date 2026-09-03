"""离线回归评测包：聚合 Stage-5 回归数据集的契约、加载、评分门禁与发布入口。

本包属于 evaluation 层，只依赖稳定数据契约与 LangSmith SDK；回归用例契约、
门禁与发布逻辑见 ``regression``，命令行发布入口见 ``publish_dataset``。
"""

from .regression import (
    REQUIRED_CATEGORIES,
    EvaluationResult,
    RegressionCase,
    RegressionGate,
    load_cases,
    publish_cases,
)

__all__ = [
    "REQUIRED_CATEGORIES",
    "EvaluationResult",
    "RegressionCase",
    "RegressionGate",
    "load_cases",
    "publish_cases",
]
