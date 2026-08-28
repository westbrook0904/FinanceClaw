"""Routing Foundation 的受限输入与类型化 Policy 约束。"""

from __future__ import annotations

from typing import Self

from harness_contracts import (
    CapabilityDescriptor,
    ContractModel,
    ExecutionMode,
    InvocationContext,
)
from harness_contracts.base import FrozenJsonMapping, FrozenJsonValue, NonEmptyString
from pydantic import Field, model_validator


class RequestSummary(ContractModel):
    """允许 Router/Planner 使用的受限 Request 投影。"""

    request_id: NonEmptyString
    input_type: NonEmptyString
    input_content: FrozenJsonValue
    target_capability: NonEmptyString | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)


class RoutePolicyConstraints(ContractModel):
    """PRE_ROUTE Policy 可产生的类型化、可安全合并的约束。"""

    forced_mode: ExecutionMode | None = None
    allowed_modes: frozenset[ExecutionMode] | None = None
    allowed_capability_ids: frozenset[NonEmptyString] | None = None
    allowed_planner_ids: frozenset[NonEmptyString] | None = None
    max_plan_attempts: int | None = Field(default=None, ge=1)
    max_plan_nodes: int | None = Field(default=None, ge=1)


class RoutingContext(ContractModel):
    """Router 可见的不可变请求、Catalog 与 Policy 上下文。"""

    invocation: InvocationContext
    request_summary: RequestSummary
    requested_mode: ExecutionMode
    catalog_snapshot: tuple[CapabilityDescriptor, ...]
    constraints: RoutePolicyConstraints = Field(default_factory=RoutePolicyConstraints)

    @model_validator(mode="after")
    def validate_request_projection_and_catalog(self) -> Self:
        request = self.invocation.request
        summary = self.request_summary
        if summary.request_id != request.request_id:
            raise ValueError("request_summary.request_id must match invocation request")
        if summary.input_type != request.input.type:
            raise ValueError("request_summary.input_type must match invocation request")

        target_capability = request.target.capability if request.target is not None else None
        if summary.target_capability != target_capability:
            raise ValueError("request_summary.target_capability must match invocation request")

        capability_ids = [descriptor.id for descriptor in self.catalog_snapshot]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("catalog_snapshot must not contain duplicate capability IDs")
        return self
