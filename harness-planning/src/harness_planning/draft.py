"""模型可生成、但不携带执行身份或运行状态的计划草案。"""

from __future__ import annotations

from typing import Literal

from harness_contracts import (
    ContractModel,
    FailurePolicy,
    FrozenOutputMapping,
    PlanBudget,
    PlanEdge,
    PlanNodeKind,
)
from harness_contracts.base import NonEmptyString
from harness_contracts.plan import FrozenBindingMapping
from pydantic import Field, model_validator


class PlanNodeDraft(ContractModel):
    """模型可填写的最小节点意图，不含重试、幂等键、超时或元数据。"""

    node_id: NonEmptyString
    kind: Literal[PlanNodeKind.CAPABILITY, PlanNodeKind.APPROVAL] = PlanNodeKind.CAPABILITY
    capability_id: NonEmptyString | None = None
    input_mapping: FrozenBindingMapping = Field(default_factory=dict)
    failure_intent: FailurePolicy = FailurePolicy.FAIL_PLAN

    @model_validator(mode="after")
    def validate_kind_fields(self) -> PlanNodeDraft:
        if self.failure_intent is FailurePolicy.FAIL_FAST:
            raise ValueError("node failure_intent cannot be fail_fast")
        if self.kind is PlanNodeKind.CAPABILITY and self.capability_id is None:
            raise ValueError("capability node draft requires capability_id")
        if self.kind is PlanNodeKind.APPROVAL and (
            self.capability_id is not None or self.input_mapping
        ):
            raise ValueError("approval node draft forbids capability_id and input_mapping")
        return self


class PlanDraft(ContractModel):
    """LLMPlanner 的受限结构化输出协议。"""

    budget: PlanBudget = Field(default_factory=PlanBudget)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    nodes: tuple[PlanNodeDraft, ...]
    edges: tuple[PlanEdge, ...] = ()
    outputs: FrozenOutputMapping = Field(default_factory=dict)
