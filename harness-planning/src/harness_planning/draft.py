"""模型可生成、但不携带执行身份或运行状态的计划草案。"""

from __future__ import annotations

from harness_contracts import (
    ContractModel,
    FailurePolicy,
    FrozenOutputMapping,
    PlanBudget,
    PlanEdge,
    PlanNode,
)
from pydantic import Field


class PlanDraft(ContractModel):
    """LLMPlanner 的受限结构化输出协议。"""

    budget: PlanBudget = Field(default_factory=PlanBudget)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...] = ()
    outputs: FrozenOutputMapping = Field(default_factory=dict)
