"""规划 attempt 与 PlanValidator 的稳定问题报告模型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal

from harness_contracts import ContractModel
from harness_contracts.base import NonEmptyString
from pydantic import Field


class PlanningAttempt(ContractModel):
    """一次模型规划 generation 的安全可观测摘要。"""

    attempt: int = Field(ge=1)
    kind: Literal["initial", "repair"]
    provider_id: NonEmptyString | None = None
    prompt_version: NonEmptyString
    output_hash: NonEmptyString | None = None
    validation_codes: tuple[NonEmptyString, ...] = ()
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    repair_scheduled: bool = False


type PlanningAttemptObserver = Callable[[PlanningAttempt], None | Awaitable[None]]


class PlanValidationCode(StrEnum):
    EMPTY_PLAN = "PLAN.EMPTY"
    INVALID_PLAN_ID = "PLAN.INVALID_PLAN_ID"
    INVALID_REVISION = "PLAN.INVALID_REVISION"
    DUPLICATE_NODE_ID = "PLAN.DUPLICATE_NODE_ID"
    INVALID_NODE_KIND = "PLAN.INVALID_NODE_KIND"
    INVALID_CAPABILITY_NODE = "PLAN.INVALID_CAPABILITY_NODE"
    INVALID_APPROVAL_NODE = "PLAN.INVALID_APPROVAL_NODE"
    INVALID_EXPLORATION_NODE = "PLAN.INVALID_EXPLORATION_NODE"
    INVALID_EXPLORATION_PLAN = "PLAN.INVALID_EXPLORATION_PLAN"
    EXPLORATION_NOT_AVAILABLE = "PLAN.EXPLORATION_NOT_AVAILABLE"
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
