"""Provider-neutral 模型结构化输出、用量遥测与 generation reservation 契约。"""

from __future__ import annotations

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
    on_unsupported: UnsupportedStructuredOutputBehavior = UnsupportedStructuredOutputBehavior.FAIL

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if (
            self.strictness is StructuredOutputStrictness.REQUIRED
            and self.on_unsupported is not UnsupportedStructuredOutputBehavior.FAIL
        ):
            raise ValueError("required structured output must fail when unsupported")
        _validate_schema_resources(self.schema)
        return self


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

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.json_schema_strict and not self.json_schema:
            raise ValueError("json_schema_strict requires json_schema")
        return self


class ModelAttemptAccounting(ContractModel):
    """单个 Provider adapter 返回的原始用量遥测。"""

    usage: ModelUsage | None = None
    complete: bool

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.complete and self.usage is None:
            raise ValueError("complete attempt accounting requires usage")
        return self


class ModelProviderAttemptUsage(ContractModel):
    """Gateway 注入可信 Provider identity 后的单 attempt 事实。"""

    provider_id: NonEmptyString
    ordinal: int = Field(ge=1)
    usage: ModelUsage | None = None
    complete: bool

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.complete and self.usage is None:
            raise ValueError("complete provider attempt accounting requires usage")
        return self


class ModelGenerationAccounting(ContractModel):
    attempts: tuple[ModelProviderAttemptUsage, ...] = Field(min_length=1)
    aggregate_usage: ModelUsage
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
    provider_incarnation: NonEmptyString
    provider_features_hash: NonEmptyString
    prepared_schema_hash: NonEmptyString
    provider_attempt: int = Field(ge=1)


class ModelGenerationReservation(ContractModel):
    generation_id: NonEmptyString
    request_fingerprint: NonEmptyString
    authorization_context_hash: NonEmptyString
    schema_hash: NonEmptyString
    registry_snapshot_hash: NonEmptyString
    slots: tuple[ModelGenerationAttemptSlot, ...] = Field(min_length=1)
    reservation_hash: NonEmptyString

    @model_validator(mode="after")
    def validate_slots(self) -> Self:
        slot_ids = [slot.slot_id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("reservation slot_id values must be unique")
        if [slot.provider_attempt for slot in self.slots] != list(range(1, len(self.slots) + 1)):
            raise ValueError("reservation provider_attempt values must be contiguous from one")
        if any(slot.prepared_schema_hash != self.schema_hash for slot in self.slots):
            raise ValueError("every reservation slot must use the reservation schema_hash")
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
