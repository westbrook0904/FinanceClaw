"""PlanValidator 的稳定问题报告模型。"""

from __future__ import annotations

from enum import StrEnum

from harness_contracts import ContractModel
from harness_contracts.base import NonEmptyString


class PlanValidationCode(StrEnum):
    EMPTY_PLAN = "PLAN.EMPTY"
    INVALID_PLAN_ID = "PLAN.INVALID_PLAN_ID"
    INVALID_REVISION = "PLAN.INVALID_REVISION"
    DUPLICATE_NODE_ID = "PLAN.DUPLICATE_NODE_ID"
    INVALID_NODE_KIND = "PLAN.INVALID_NODE_KIND"
    INVALID_CAPABILITY_NODE = "PLAN.INVALID_CAPABILITY_NODE"
    INVALID_APPROVAL_NODE = "PLAN.INVALID_APPROVAL_NODE"
    INVALID_TIMEOUT = "PLAN.INVALID_TIMEOUT"
    INVALID_RETRY_POLICY = "PLAN.INVALID_RETRY_POLICY"
    INVALID_DEADLINE = "PLAN.INVALID_DEADLINE"
    INVALID_FAILURE_POLICY = "PLAN.INVALID_FAILURE_POLICY"
    EDGE_SOURCE_NOT_FOUND = "PLAN.EDGE_SOURCE_NOT_FOUND"
    EDGE_TARGET_NOT_FOUND = "PLAN.EDGE_TARGET_NOT_FOUND"
    SELF_EDGE = "PLAN.SELF_EDGE"
    NO_ROOT = "PLAN.NO_ROOT"
    CYCLE = "PLAN.CYCLE"
    INVALID_BINDING = "PLAN.INVALID_BINDING"
    INPUT_REFERENCE_NOT_FOUND = "PLAN.INPUT_REFERENCE_NOT_FOUND"
    INPUT_REFERENCE_UNAVAILABLE = "PLAN.INPUT_REFERENCE_UNAVAILABLE"
    INVALID_OUTPUT = "PLAN.INVALID_OUTPUT"
    OUTPUT_REFERENCE_NOT_FOUND = "PLAN.OUTPUT_REFERENCE_NOT_FOUND"
    INVALID_CONDITION = "PLAN.INVALID_CONDITION"
    CONDITION_REFERENCE_NOT_FOUND = "PLAN.CONDITION_REFERENCE_NOT_FOUND"
    CONDITION_REFERENCE_UNAVAILABLE = "PLAN.CONDITION_REFERENCE_UNAVAILABLE"
    CAPABILITY_NOT_FOUND = "PLAN.CAPABILITY_NOT_FOUND"
    INVALID_CAPABILITY_DESCRIPTOR = "PLAN.INVALID_CAPABILITY_DESCRIPTOR"


class PlanValidationIssue(ContractModel):
    """一个可序列化、可定位的计划校验问题。"""

    code: PlanValidationCode
    message: NonEmptyString
    node_id: NonEmptyString | None = None
    edge_index: int | None = None
    field: NonEmptyString | None = None
    reference: NonEmptyString | None = None


class PlanValidationError(ValueError):
    """PlanValidator 聚合所有可确定问题后抛出的异常。"""

    def __init__(self, issues: tuple[PlanValidationIssue, ...]) -> None:
        if not issues:
            raise ValueError("PlanValidationError requires at least one issue")
        self.issues = issues
        summary = "; ".join(f"{issue.code.value}: {issue.message}" for issue in issues)
        super().__init__(f"execution plan validation failed: {summary}")
