"""Strict generation 的进程内 prepared objects 与 fencing 协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from harness_contracts import (
    ContractModel,
    ModelGenerationAttemptSlot,
    ModelGenerationReservation,
    ModelProviderAttemptUsage,
    ModelReservationReceipt,
    ModelSlotExecutionTicket,
    RetryPolicy,
)
from harness_registry import ProviderRegistration
from pydantic import Field

from .contracts import GenerateRequest


@dataclass(frozen=True, slots=True)
class PreparedStructuredOutput:
    """Provider adapter 本地编译的 opaque strict-schema 配置。"""

    provider_id: str
    schema_hash: str
    semantics_preserved: bool
    payload: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise TypeError("provider_id must be a non-empty string")
        if not isinstance(self.schema_hash, str) or not self.schema_hash.strip():
            raise TypeError("schema_hash must be a non-empty string")
        if not isinstance(self.semantics_preserved, bool):
            raise TypeError("semantics_preserved must be bool")


class ModelAttemptPolicy(ContractModel):
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    allow_fallback: bool = True
    max_provider_count: int = Field(default=8, ge=1)
    max_input_tokens_per_call: int | None = Field(default=None, ge=1)
    require_complete_accounting: bool = True
    require_cost_bounds: bool = False


@dataclass(frozen=True, slots=True)
class PreparedModelGeneration:
    """仅在当前进程存活的 reservation 与 opaque prepared-slot 绑定。"""

    request: GenerateRequest = field(repr=False)
    reservation: ModelGenerationReservation
    registrations: tuple[ProviderRegistration, ...] = field(repr=False)
    prepared_by_slot: Mapping[str, PreparedStructuredOutput] = field(repr=False)
    attempt_policy: ModelAttemptPolicy
    process_nonce: str
    _prepared_snapshot: Mapping[str, PreparedStructuredOutput] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, GenerateRequest):
            raise TypeError("request must be GenerateRequest")
        if not isinstance(self.reservation, ModelGenerationReservation):
            raise TypeError("reservation must be ModelGenerationReservation")
        if not isinstance(self.attempt_policy, ModelAttemptPolicy):
            raise TypeError("attempt_policy must be ModelAttemptPolicy")
        if not isinstance(self.process_nonce, str) or not self.process_nonce.strip():
            raise TypeError("process_nonce must be a non-empty string")
        if any(not isinstance(item, ProviderRegistration) for item in self.registrations):
            raise TypeError("registrations must contain ProviderRegistration values")
        snapshot = MappingProxyType(dict(self.prepared_by_slot))
        expected = {slot.slot_id for slot in self.reservation.slots}
        if set(snapshot) != expected:
            raise ValueError("prepared slot mapping must match reservation slots")
        registration_ids = {item.provider_id for item in self.registrations}
        slot_provider_ids = {slot.provider_id for slot in self.reservation.slots}
        if registration_ids != slot_provider_ids:
            raise ValueError("registrations must match reservation providers")
        for slot in self.reservation.slots:
            prepared = snapshot[slot.slot_id]
            if prepared.provider_id != slot.provider_id:
                raise ValueError("prepared slot belongs to another provider")
            if prepared.schema_hash != slot.prepared_schema_hash:
                raise ValueError("prepared slot schema hash does not match reservation")
            if not prepared.semantics_preserved:
                raise ValueError("prepared slot must preserve schema semantics")
        object.__setattr__(self, "prepared_by_slot", snapshot)
        object.__setattr__(self, "_prepared_snapshot", snapshot)


@runtime_checkable
class ModelGenerationCheckpointSink(Protocol):
    """Exploration checkpoint 层提供的 slot STARTED/terminal CAS 边界。"""

    async def start_model_generation_slot(
        self,
        receipt: ModelReservationReceipt,
        reservation: ModelGenerationReservation,
        slot: ModelGenerationAttemptSlot,
    ) -> ModelSlotExecutionTicket: ...

    async def complete_model_generation_slot(
        self,
        receipt: ModelReservationReceipt,
        ticket: ModelSlotExecutionTicket,
        accounting: ModelProviderAttemptUsage,
        outcome: Literal["completed", "failed", "orphaned"],
    ) -> None: ...
