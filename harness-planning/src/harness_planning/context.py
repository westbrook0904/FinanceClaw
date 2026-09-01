"""Planner 可见的受限目标、Catalog 与规划约束。"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from harness_contracts import (
    CapabilityDescriptor,
    ContextConsumer,
    ContextProjection,
    ContextUseRecord,
    ContractModel,
    InvocationContext,
)
from harness_contracts.base import NonEmptyString
from harness_routing import RequestSummary
from pydantic import Field, field_validator, model_validator


class PlanningConstraints(ContractModel):
    """一次规划调用的确定性大小、能力范围与 Deadline 边界。"""

    max_plan_attempts: int = Field(default=3, ge=1)
    max_plan_nodes: int = Field(default=32, ge=1)
    allowed_capability_ids: frozenset[NonEmptyString] | None = None
    deadline_at: datetime | None = None

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("planning deadline must include timezone information")
        return value


class PlanningContext(ContractModel):
    """Planner 的不可变输入，不暴露 Provider、插件实例或执行状态。"""

    invocation: InvocationContext
    goal: RequestSummary
    catalog_snapshot: tuple[CapabilityDescriptor, ...]
    constraints: PlanningConstraints = Field(default_factory=PlanningConstraints)
    projection: ContextProjection | None = None
    context_use: ContextUseRecord | None = None

    @model_validator(mode="after")
    def validate_goal_and_catalog(self) -> Self:
        request = self.invocation.request
        if self.goal.request_id != request.request_id:
            raise ValueError("goal.request_id must match invocation request")
        if self.goal.input_type != request.input.type:
            raise ValueError("goal.input_type must match invocation request")

        target_capability = request.target.capability if request.target is not None else None
        if self.goal.target_capability != target_capability:
            raise ValueError("goal.target_capability must match invocation request")

        capability_ids = [descriptor.id for descriptor in self.catalog_snapshot]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("catalog_snapshot must not contain duplicate capability IDs")

        if (self.projection is None) != (self.context_use is None):
            raise ValueError("planning projection and context_use must be supplied together")
        if self.projection is not None and self.context_use is not None:
            if self.projection.consumer is not ContextConsumer.PLAN:
                raise ValueError("planning context requires a plan projection")
            if self.context_use.consumer is not ContextConsumer.PLAN:
                raise ValueError("planning context requires a plan context use record")
            if self.context_use.snapshot_id != self.projection.snapshot_id:
                raise ValueError("planning projection and context_use snapshot IDs must match")
            if self.context_use.projection_hash != self.projection.projection_hash:
                raise ValueError("planning projection and context_use hashes must match")
            if self.context_use.included_item_ids != tuple(
                item.item_id for item in self.projection.items
            ):
                raise ValueError("planning projection and context_use items must match")
            if self.context_use.omitted != self.projection.omitted:
                raise ValueError("planning projection and context_use omissions must match")
        return self
