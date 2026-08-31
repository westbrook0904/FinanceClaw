"""Provider-neutral 模型结构化输出、计费与 generation reservation 契约。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import ContractModel, FrozenJsonMapping, NonEmptyString

MAX_STRUCTURED_SCHEMA_DEPTH = 32
MAX_STRUCTURED_SCHEMA_NODES = 2_048
MAX_STRUCTURED_SCHEMA_ENUM_VALUES = 256
MAX_STRUCTURED_SCHEMA_STRING_LENGTH = 16_384


class StructuredOutputStrictness(StrEnum):
    BEST_EFFORT = "best_effort"
    REQUIRED = "required"


class UnsupportedStructuredOutputBehavior(StrEnum):
    FAIL = "fail"
    JSON_OBJECT = "json_object"


class StructuredOutputSpec(ContractModel):
    """Provider-neutral JSON Schema 输出要求。"""

    name: NonEmptyString
    schema: FrozenJsonMapping
    strictness: StructuredOutputStrictness = StructuredOutputStrictness.REQUIRED
    on_unsupported: UnsupportedStructuredOutputBehavior = (
        UnsupportedStructuredOutputBehavior.FAIL
    )

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if (
            self.strictness is StructuredOutputStrictness.REQUIRED
            and self.on_unsupported is not UnsupportedStructuredOutputBehavior.FAIL
        ):
            raise ValueError("required structured output must fail when unsupported")
        _validate_schema_resources(self.schema)
        return self


class NormalizedCost(ContractModel):
    unit: NonEmptyString
    amount: float = Field(ge=0, allow_inf_nan=False)


class NormalizedCostRate(ContractModel):
    unit: NonEmptyString
    max_input_token_cost: float = Field(ge=0, allow_inf_nan=False)
    max_output_token_cost: float = Field(ge=0, allow_inf_nan=False)
    max_request_cost: float = Field(default=0, ge=0, allow_inf_nan=False)


class ModelUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ModelProviderFeatures(ContractModel):
    json_object: bool = False
    json_schema: bool = False
    json_schema_strict: bool = False
    refusal_signal: bool = False
    usage_tokens: bool = True
    normalized_cost: bool = False
    cost_rate: NormalizedCostRate | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.json_schema_strict and not self.json_schema:
            raise ValueError("json_schema_strict requires json_schema")
        if self.normalized_cost != (self.cost_rate is not None):
            raise ValueError("normalized_cost and cost_rate must be configured together")
        return self


class ModelAttemptAccounting(ContractModel):
    """单个 Provider adapter 返回的原始计费事实。"""

    usage: ModelUsage | None = None
    normalized_cost: NormalizedCost | None = None
    complete: bool


class ModelProviderAttemptUsage(ContractModel):
    """Gateway 注入可信 Provider identity 后的单 attempt 事实。"""

    provider_id: NonEmptyString
    ordinal: int = Field(ge=1)
    usage: ModelUsage | None = None
    normalized_cost: NormalizedCost | None = None
    complete: bool


class ModelGenerationAccounting(ContractModel):
    attempts: tuple[ModelProviderAttemptUsage, ...]
    aggregate_usage: ModelUsage
    aggregate_cost: NormalizedCost | None = None
    complete: bool

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        ordinals = [attempt.ordinal for attempt in self.attempts]
        if ordinals != list(range(1, len(self.attempts) + 1)):
            raise ValueError("model accounting ordinals must be contiguous from one")
        expected_input = sum(
            attempt.usage.input_tokens for attempt in self.attempts if attempt.usage is not None
        )
        expected_output = sum(
            attempt.usage.output_tokens for attempt in self.attempts if attempt.usage is not None
        )
        if self.aggregate_usage != ModelUsage(
            input_tokens=expected_input,
            output_tokens=expected_output,
            total_tokens=expected_input + expected_output,
        ):
            raise ValueError("aggregate_usage must equal the sum of attempt usage")
        costs = [
            attempt.normalized_cost
            for attempt in self.attempts
            if attempt.normalized_cost is not None
        ]
        if costs:
            units = {cost.unit for cost in costs}
            if len(units) != 1:
                raise ValueError("attempt normalized cost units must match")
            if self.aggregate_cost is None or self.aggregate_cost.unit not in units:
                raise ValueError("aggregate_cost must use the attempt cost unit")
            if not math.isclose(
                self.aggregate_cost.amount,
                sum(cost.amount for cost in costs),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("aggregate_cost must equal the sum of attempt costs")
        elif self.aggregate_cost is not None:
            raise ValueError("aggregate_cost requires at least one attempt cost")
        if self.complete != all(attempt.complete for attempt in self.attempts):
            raise ValueError("generation accounting completeness must match all attempts")
        return self


class PlanNodeRef(ContractModel):
    kind: Literal["plan_node"] = "plan_node"
    plan_id: NonEmptyString
    node_id: NonEmptyString


class ModelGenerationAttemptSlot(ContractModel):
    slot_id: NonEmptyString
    provider_id: NonEmptyString
    provider_registration_version: NonEmptyString
    provider_features_hash: NonEmptyString
    prepared_schema_hash: NonEmptyString
    provider_attempt: int = Field(ge=1)
    input_token_upper_bound: int = Field(ge=0)
    output_token_upper_bound: int = Field(ge=0)
    token_upper_bound: int = Field(ge=0)
    normalized_cost_upper_bound: NormalizedCost | None = None

    @model_validator(mode="after")
    def validate_token_total(self) -> Self:
        if self.token_upper_bound != (
            self.input_token_upper_bound + self.output_token_upper_bound
        ):
            raise ValueError("token_upper_bound must equal input plus output bounds")
        return self


class ModelGenerationReservation(ContractModel):
    generation_id: NonEmptyString
    request_fingerprint: NonEmptyString
    schema_hash: NonEmptyString
    registry_snapshot_hash: NonEmptyString
    slots: tuple[ModelGenerationAttemptSlot, ...] = Field(min_length=1)
    total_token_upper_bound: int = Field(ge=0)
    total_cost_upper_bound: NormalizedCost | None = None
    reservation_hash: NonEmptyString

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        slot_ids = [slot.slot_id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("reservation slot_id values must be unique")
        if [slot.provider_attempt for slot in self.slots] != list(
            range(1, len(self.slots) + 1)
        ):
            raise ValueError("reservation provider_attempt values must be contiguous from one")
        if any(slot.prepared_schema_hash != self.schema_hash for slot in self.slots):
            raise ValueError("every reservation slot must use the reservation schema_hash")
        if self.total_token_upper_bound != sum(slot.token_upper_bound for slot in self.slots):
            raise ValueError("total_token_upper_bound must equal all slot bounds")
        costs = [
            slot.normalized_cost_upper_bound
            for slot in self.slots
            if slot.normalized_cost_upper_bound is not None
        ]
        if costs:
            units = {cost.unit for cost in costs}
            if len(units) != 1:
                raise ValueError("reservation cost units must match")
            if len(costs) != len(self.slots):
                raise ValueError("reservation cost bounds must be complete or absent")
            if self.total_cost_upper_bound is None or self.total_cost_upper_bound.unit not in units:
                raise ValueError("total_cost_upper_bound must use the slot cost unit")
            if not math.isclose(
                self.total_cost_upper_bound.amount,
                sum(cost.amount for cost in costs),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("total_cost_upper_bound must equal all slot cost bounds")
        elif self.total_cost_upper_bound is not None:
            raise ValueError("total_cost_upper_bound requires per-slot bounds")
        return self


class ModelReservationReceipt(ContractModel):
    execution_ref: PlanNodeRef
    exploration_id: NonEmptyString
    generation_id: NonEmptyString
    reservation_hash: NonEmptyString
    committed_state_version: int = Field(ge=0)
    scheduler_generation: int = Field(ge=0)
    owner_epoch: int = Field(ge=0)


class ModelSlotExecutionTicket(ContractModel):
    generation_id: NonEmptyString
    slot_id: NonEmptyString
    reservation_hash: NonEmptyString
    committed_state_version: int = Field(ge=0)
    scheduler_generation: int = Field(ge=0)
    owner_epoch: int = Field(ge=0)


def _validate_schema_resources(schema: object) -> None:
    nodes = 0

    def visit(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_STRUCTURED_SCHEMA_DEPTH:
            raise ValueError("structured output schema exceeds maximum depth")
        if nodes > MAX_STRUCTURED_SCHEMA_NODES:
            raise ValueError("structured output schema exceeds maximum nodes")
        if isinstance(value, str):
            if len(value) > MAX_STRUCTURED_SCHEMA_STRING_LENGTH:
                raise ValueError("structured output schema string is too long")
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if len(key) > MAX_STRUCTURED_SCHEMA_STRING_LENGTH:
                    raise ValueError("structured output schema key is too long")
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(item, str) or not item.startswith("#"):
                        raise ValueError("remote JSON Schema references are forbidden")
                if key == "enum" and isinstance(item, tuple | list):
                    if len(item) > MAX_STRUCTURED_SCHEMA_ENUM_VALUES:
                        raise ValueError("structured output schema enum is too large")
                visit(item, depth + 1)
            return
        if isinstance(value, tuple | list):
            for item in value:
                visit(item, depth + 1)

    visit(schema, 1)
